"""The audit layer, end to end: make a few things happen, then find each of them in the audit views.
  1. a test user reads a secured view (on the SQL warehouse, and on the Real-Time warehouse if there is one)
  2. a test user tries to read a base table directly, to create a view in the views schema and to copy a view into
     a table (all refused)
  3. the admin reads a base table directly, and grants a privilege by hand that the guard then revokes
Then, as the admin (who may read the audit schema, like the auditors), it waits until the system tables have caught up
(measured: the query history after about 10 minutes, the audit log after 16 to 22; AUDIT_WAIT_MINUTES,
default 30) and checks every event in the audit views.
Run: python3 -I tests/test_audit.py"""
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config  # noqa: E402
import dbsql  # noqa: E402

C, B, S, G, A = config.CATALOG, f"{config.CATALOG}.{config.BASE}", f"{config.CATALOG}.{config.SECURE}", \
    f"{config.CATALOG}.{config.GOV}", f"{config.CATALOG}.{config.AUDIT}"
V = f"{S}.associate_compensation{config.VIEW_SUFFIX}"
READER, INTRUDER, GRANTEE = "people_mgr", "mm_market_abc", "no_persona"
WAIT = int(os.environ.get("AUDIT_WAIT_MINUTES", "30"))
admin = dbsql.client()


def a(sql):
    return dbsql.run(admin, sql, quiet=True)[1]


def on(warehouse, who, sql):
    """Run one statement on a given warehouse (dbsql caches the warehouse it found first)."""
    saved, old = dict(dbsql._wh), os.environ.get("POC_WAREHOUSE")
    dbsql._wh.clear()
    os.environ["POC_WAREHOUSE"] = warehouse
    try:
        return dbsql.run(dbsql.client(who), sql, quiet=True)[1]
    finally:
        dbsql._wh.clear()
        dbsql._wh.update(saved)
        if old is None:
            os.environ.pop("POC_WAREHOUSE", None)
        else:
            os.environ["POC_WAREHOUSE"] = old


def refused(who, sql):
    try:
        dbsql.run(dbsql.client(who), sql, quiet=True)
        return False
    except RuntimeError:
        return True


def main():
    tag = random.randint(10**8, 10**9 - 1)  # makes every statement unique (no result cache) and findable
    reader, intruder, grantee = (config.app_id(k) for k in (READER, INTRUDER, GRANTEE))
    t0 = a("SELECT CAST(current_timestamp() - INTERVAL 1 MINUTE AS STRING)")[0][0]
    since = f"TIMESTAMP '{t0}'"
    print(f"tag {tag}; events from {t0}")

    # 1. reads of a secured view
    dbsql.run(dbsql.client(READER), f"SELECT count(*), sum(bonus) FROM {V} WHERE win <> {tag}", quiet=True)
    rt = config.RT_WAREHOUSE_NAME and any(x.name == config.RT_WAREHOUSE_NAME for x in admin.warehouses.list())
    if rt:
        on(config.RT_WAREHOUSE_NAME, READER, f"SELECT count(*), sum(bonus) FROM {V} WHERE win <> {tag + 1}")
    # 2. refused attempts
    assert refused(INTRUDER, f"SELECT count(*) FROM {B}.associate_compensation WHERE win <> {tag}"), "base table readable"
    probe = f"audit_probe_{tag}"
    assert refused(INTRUDER, f"CREATE VIEW {S}.{probe} AS SELECT 1 AS x"), "a user could create a view"
    assert refused(INTRUDER, f"CREATE TABLE {S}.{probe}_copy AS SELECT * FROM {V}"), "a user could copy a view"
    # 3. the admin: a direct read, and a stray grant that the guard takes back
    a(f"SELECT count(*) FROM {B}.associate_compensation WHERE win <> {tag}")
    a(f"GRANT SELECT ON TABLE {G}.compile_log TO `{grantee}`")
    subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "compiler",
                    "fgac_compiler.py"), "guard", "--as", "compiler"], check=True, stdout=subprocess.DEVNULL)
    personas = a(f"SELECT to_json(personas) FROM {G}.user_entitlement WHERE user_principal = '{reader}'")[0][0]

    day = f"event_date >= CAST({since} AS DATE)"
    checks = {
        f"view_reads: {READER} read {V.split('.')[-1]}, with the personas they held then": f"""
            SELECT count(*) FROM {A}.view_reads WHERE {day} AND read_at >= {since} AND reader = '{reader}'
              AND view_name = '{V}' AND base_table = '{B}.associate_compensation' AND status_code = 200
              AND to_json(personas_then) = '{personas}'""",
        "statements: the same read, with its SQL text and the user": f"""
            SELECT count(*) FROM {A}.statements WHERE start_time >= {since} AND executed_by = '{reader}'
              AND contains(statement_text, 'win <> {tag}') AND execution_status = 'FINISHED' AND NOT by_compiler""",
        f"statements: {INTRUDER}'s refused read of the base table": f"""
            SELECT count(*) FROM {A}.statements WHERE start_time >= {since} AND executed_by = '{intruder}'
              AND contains(statement_text, '{config.BASE}.associate_compensation WHERE win <> {tag}')
              AND execution_status = 'FAILED'""",
        f"governance_changes: {INTRUDER}'s refused attempt to create a view": f"""
            SELECT count(*) FROM {A}.governance_changes WHERE {day} AND changed_at >= {since} AND changed_by = '{intruder}'
              AND object_name = '{S}.{probe}' AND change = 'create' AND outcome = 'denied'""",
        "governance_changes: the admin's stray grant (not the compiler)": f"""
            SELECT count(*) FROM {A}.governance_changes WHERE {day} AND changed_at >= {since}
              AND changed_by = '{config.ADMIN}' AND NOT by_compiler AND change = 'privileges'
              AND object_name = '{G}.compile_log' AND contains(detail, '{grantee}') AND contains(detail, 'add')
              AND outcome = 'done'""",
        "governance_changes: the guard (the compiler) revoking it": f"""
            SELECT count(*) FROM {A}.governance_changes WHERE {day} AND changed_at >= {since} AND by_compiler
              AND change = 'privileges' AND object_name = '{G}.compile_log' AND contains(detail, '{grantee}')
              AND contains(detail, 'remove') AND outcome = 'done'""",
        "restricted_reads: the admin's direct read of a base table": f"""
            SELECT count(*) FROM {A}.restricted_reads WHERE {day} AND read_at >= {since} AND reader = '{config.ADMIN}'
              AND (object_name = '{B}.associate_compensation' OR contains(statement_text, 'win <> {tag}'))""",
        f"restricted_reads: {INTRUDER}'s refused read of the base table": f"""
            SELECT count(*) FROM {A}.restricted_reads WHERE {day} AND read_at >= {since} AND reader = '{intruder}'
              AND contains(statement_text, 'win <> {tag}') AND execution_status = 'FAILED'""",
        f"governance_changes: {INTRUDER}'s refused copy of the view into a table": f"""
            SELECT count(*) FROM {A}.governance_changes WHERE {day} AND changed_at >= {since} AND changed_by = '{intruder}'
              AND object_name = '{S}.{probe}_copy' AND outcome = 'denied'""",
        "compile_runs: the guard's run that revoked the grant": f"""
            SELECT count(*) FROM {A}.compile_runs WHERE run_at >= {since} AND step = 'guard'
              AND contains(detail, '{grantee}')""",
        f"entitlement_changes: {READER}'s current entitlement": f"""
            SELECT count(*) FROM {A}.entitlement_changes WHERE user_principal = '{reader}' AND is_current
              AND to_json(personas) = '{personas}'""",
    }
    if rt:
        checks["statements: the read on the Real-Time warehouse"] = f"""
            SELECT count(*) FROM {A}.statements s WHERE start_time >= {since} AND executed_by = '{reader}'
              AND contains(statement_text, 'win <> {tag + 1}') AND warehouse_id = '{
              next(x.id for x in admin.warehouses.list() if x.name == config.RT_WAREHOUSE_NAME)}'"""
        checks[f"view_reads: both reads by {READER} (SQL warehouse and Real-Time)"] = f"""
            SELECT CASE WHEN count(*) >= 2 THEN 1 ELSE 0 END FROM {A}.view_reads WHERE {day} AND read_at >= {since}
              AND reader = '{reader}' AND view_name = '{V}'"""
    found, start = {}, time.time()
    while True:
        for name, sql in checks.items():
            if name not in found and int(a(sql)[0][0]) > 0:
                found[name] = time.time() - start
        if len(found) == len(checks) or time.time() - start > WAIT * 60:
            break
        print(f"    {len(found)} of {len(checks)} found after {(time.time() - start) / 60:.0f} min; waiting for the system tables")
        time.sleep(60)
    failures = 0
    for name in checks:
        ok = name in found
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  (after {found[name] / 60:.0f} min)" if ok else f"  (not found in {WAIT} min)"))
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
