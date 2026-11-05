"""Governance invariants, run as the last task of every compiler job (and by hand: --as compiler). Raises (failing
the job) if users could see more than the policies allow, or if Databricks is behind the policy exports."""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(globals().get("__file__") or sys.argv[0])), ".."))
import config  # noqa: E402
import dbsql  # noqa: E402

C, X, G, BASE, SECURE = config.CATALOG, f"{config.CATALOG}.{config.EXPORTS}", f"{config.CATALOG}.{config.GOV}", config.BASE, config.SECURE
AUDIT = config.AUDIT
GRACE_MINUTES = int(os.environ.get("INVARIANTS_GRACE_MINUTES", "30"))  # changes must show within 30 minutes
SOURCES = config.EXPORT_TABLES
SFX = config.VIEW_SUFFIX
LEFT = config.TERMINATED_SQL  # assoc_dim.status of leavers
# the role rows that count, as the compiler reads them: with a login, of a persona with exactly one scope type
ROLES = f"""(SELECT lower(r.user_principal) AS user_principal, r.win FROM {X}.assoc_roles_dim r
           JOIN (SELECT persona_id FROM {X}.persona GROUP BY persona_id HAVING count(DISTINCT scope_type) = 1) p USING (persona_id)
           WHERE r.user_principal IS NOT NULL)"""
LEAVERS = f"""(SELECT DISTINCT a.user_principal FROM {ROLES} a JOIN {X}.assoc_dim d ON d.win = a.win
             WHERE d.status IN ({LEFT}))"""  # a login any of whose wins has left
# run as the compiler (the job's last task, or --as compiler): session_user() is the identity that must own everything,
# not whoever owns the catalog now (an owner change must not make itself the reference)
ALLOWED = f"""session_user(), '{config.ADMIN}'"""  # on the catalog, the entitlements and the views schema: nobody else
# as the guard: the data loaders may also hold privileges on the two schemas they load
LOADERS = ALLOWED + "".join(f", '{p}'" for p in config.ETL_PRINCIPALS)
# '' (no principal has that name), never NULL: x NOT IN (NULL) is NULL, which would hide every violation
AUDITORS = ", ".join("'" + p.replace("'", "''") + "'" for p in config.AUDITORS) or "''"  # may read the audit schema

CHECKS = {
    "every classified base table has its secured view": f"""
        WITH e AS (SELECT split(lower(qualified_column_name), '[.]') AS p FROM {X}.column_classification
                   WHERE size(split(qualified_column_name, '[.]')) = 4)  -- malformed names are skipped, as by the compiler
        SELECT DISTINCT t.table_name FROM e
        JOIN {C}.information_schema.tables t ON e.p[0] = '{C}' AND e.p[1] = '{BASE}' AND t.table_schema = '{BASE}'
             AND t.table_name = e.p[2]
        WHERE NOT EXISTS (SELECT 1 FROM {C}.information_schema.views v WHERE v.table_schema = '{SECURE}'
                          AND v.table_name = concat(t.table_name, '{SFX}'))""",
    "every secured view is exactly as the compiler generated it": f"""
        SELECT g.view_name || CASE WHEN v.table_name IS NULL THEN ' missing' ELSE ' edited' END FROM {G}.generated_views g
        LEFT JOIN {C}.information_schema.views v ON v.table_schema = '{SECURE}' AND v.table_name = g.view_name
        WHERE v.table_name IS NULL OR sha2(v.view_definition, 256) <> g.stored_sha""",
    "the views schema holds only generated views": f"""
        SELECT t.table_name FROM {C}.information_schema.tables t WHERE t.table_schema = '{SECURE}'
          AND t.table_name NOT IN (SELECT view_name FROM {G}.generated_views)
        UNION ALL
        SELECT 'function ' || specific_name FROM {C}.information_schema.routines WHERE specific_schema = '{SECURE}'
        UNION ALL
        SELECT 'volume ' || volume_name FROM {C}.information_schema.volumes WHERE volume_schema = '{SECURE}'""",
    "the catalog, its schemas and their objects are owned by the compiler (data loaders may own what they load)": f"""
        SELECT 'catalog owned by ' || catalog_owner FROM {C}.information_schema.catalogs
        WHERE catalog_name = '{C}' AND catalog_owner <> session_user()
        UNION ALL
        SELECT 'schema ' || schema_name || ' owned by ' || schema_owner FROM {C}.information_schema.schemata
        WHERE schema_name IN ('{BASE}', '{X.split('.')[1]}', '{G.split('.')[1]}', '{SECURE}', '{AUDIT}') AND schema_owner <> session_user()
        UNION ALL
        SELECT table_schema || '.' || table_name || ' owned by ' || table_owner FROM {C}.information_schema.tables
        WHERE table_schema IN ('{BASE}', '{X.split('.')[1]}', '{G.split('.')[1]}', '{SECURE}', '{AUDIT}') AND table_owner <> session_user()
          AND NOT (table_schema IN ('{BASE}', '{X.split('.')[1]}') AND table_owner IN ({", ".join("'" + p + "'" for p in config.ETL_PRINCIPALS) or "''"}))""",
    "nobody else can read the base tables, the exports or the entitlements (tables, volumes, functions)": f"""
        SELECT grantee || ' has ' || privilege_type || ' on ' || schema_name FROM {C}.information_schema.schema_privileges
        WHERE schema_name IN ('{BASE}', '{config.EXPORTS}', '{config.GOV}')
          AND grantee NOT IN ({ALLOWED}) AND (schema_name = '{config.GOV}' OR grantee NOT IN ({LOADERS}))
        UNION ALL
        SELECT grantee || ' has ' || privilege_type || ' on ' || table_schema || '.' || table_name
        FROM {C}.information_schema.table_privileges
        WHERE table_schema IN ('{BASE}', '{config.EXPORTS}', '{config.GOV}')
          AND grantee NOT IN ({ALLOWED}) AND (table_schema = '{config.GOV}' OR grantee NOT IN ({LOADERS}))
        UNION ALL
        SELECT grantee || ' has ' || privilege_type || ' on volume ' || volume_schema || '.' || volume_name
        FROM {C}.information_schema.volume_privileges
        WHERE volume_schema IN ('{BASE}', '{config.EXPORTS}', '{config.GOV}')
          AND grantee NOT IN ({ALLOWED}) AND (volume_schema = '{config.GOV}' OR grantee NOT IN ({LOADERS}))
        UNION ALL
        SELECT grantee || ' has ' || privilege_type || ' on function ' || specific_schema || '.' || specific_name
        FROM {C}.information_schema.routine_privileges
        WHERE specific_schema IN ('{BASE}', '{config.EXPORTS}', '{config.GOV}')
          AND grantee NOT IN ({ALLOWED}) AND (specific_schema = '{config.GOV}' OR grantee NOT IN ({LOADERS}))
        UNION ALL
        SELECT grantee || ' has ' || privilege_type || ' on the catalog' FROM {C}.information_schema.catalog_privileges
        WHERE catalog_name = '{C}' AND privilege_type <> 'USE_CATALOG' AND grantee NOT IN ({ALLOWED})""",
    "users can only read the views schema, not write to it or manage a view": f"""
        SELECT grantee || ' has ' || privilege_type || ' on {SECURE}' FROM {C}.information_schema.schema_privileges
        WHERE schema_name = '{SECURE}' AND privilege_type NOT IN ('USE_SCHEMA', 'SELECT') AND grantee NOT IN ({ALLOWED})
        UNION ALL
        SELECT grantee || ' has ' || privilege_type || ' on view ' || table_name FROM {C}.information_schema.table_privileges
        WHERE table_schema = '{SECURE}' AND inherited_from = 'NONE' AND grantee NOT IN ({ALLOWED})""",
    "the audit schema holds exactly the audit views, as the compiler generated them": f"""
        SELECT n || ' missing' FROM VALUES {", ".join(f"('{v}')" for v in config.AUDIT_VIEWS)} AS t(n)
        WHERE n NOT IN (SELECT view_name FROM {G}.audit_views)
        UNION ALL
        SELECT a.view_name || CASE WHEN v.table_name IS NULL THEN ' missing' ELSE ' edited' END FROM {G}.audit_views a
        LEFT JOIN {C}.information_schema.views v ON v.table_schema = '{AUDIT}' AND v.table_name = a.view_name
        WHERE v.table_name IS NULL OR sha2(v.view_definition, 256) <> a.stored_sha
        UNION ALL
        SELECT t.table_name FROM {C}.information_schema.tables t WHERE t.table_schema = '{AUDIT}'
          AND t.table_name NOT IN (SELECT view_name FROM {G}.audit_views)
        UNION ALL
        SELECT 'function ' || specific_name FROM {C}.information_schema.routines WHERE specific_schema = '{AUDIT}'
        UNION ALL
        SELECT 'volume ' || volume_name FROM {C}.information_schema.volumes WHERE volume_schema = '{AUDIT}'""",
    "only the auditors can read the audit views, and nobody else can change them": f"""
        SELECT grantee || ' has ' || privilege_type || ' on {AUDIT}' FROM {C}.information_schema.schema_privileges
        WHERE schema_name = '{AUDIT}' AND grantee NOT IN ({ALLOWED})
          AND (grantee NOT IN ({AUDITORS}) OR privilege_type NOT IN ('USE_SCHEMA', 'SELECT'))
        UNION ALL
        SELECT grantee || ' has ' || privilege_type || ' on ' || table_name FROM {C}.information_schema.table_privileges
        WHERE table_schema = '{AUDIT}' AND inherited_from = 'NONE' AND grantee NOT IN ({ALLOWED})""",
    "one entitlement row per user": f"""
        SELECT user_principal || ' has ' || count(*) || ' rows' FROM {G}.user_entitlement GROUP BY user_principal HAVING count(*) > 1""",
    "the policy exports are consistent (rows that break these rules grant nothing until fixed)": f"""
        SELECT 'assoc_roles_dim: ' || count(*) || ' row(s) without a user_principal' FROM {X}.assoc_roles_dim
        WHERE user_principal IS NULL HAVING count(*) > 0
        UNION ALL
        SELECT 'assoc_roles_dim: ' || lower(user_principal) || ' has ' || count(DISTINCT win) || ' different wins (no Self until fixed)'
        FROM {X}.assoc_roles_dim WHERE user_principal IS NOT NULL GROUP BY lower(user_principal) HAVING count(DISTINCT win) > 1
        UNION ALL
        SELECT 'persona: ' || persona_id || ' has ' || count(DISTINCT scope_type) || ' scope types (grants nothing until fixed)'
        FROM {X}.persona GROUP BY persona_id HAVING count(DISTINCT scope_type) > 1
        UNION ALL
        SELECT 'persona_column_policy: ' || coalesce(persona_id, 'NULL') || ' has a row without a classification or a category'
        FROM {X}.persona_column_policy WHERE classification IS NULL OR data_security_category IS NULL""",
    "the live entitlements cover exactly the users in the exports": f"""
        SELECT coalesce(r.user_principal, e.user_principal) || CASE WHEN e.user_principal IS NULL THEN ' not compiled'
                                                                    ELSE ' no longer in the exports' END
        FROM (SELECT DISTINCT a.user_principal  -- the compiler's rules: a login, an unambiguous persona, not left
              FROM {ROLES} a LEFT ANTI JOIN {LEAVERS} l USING (user_principal)) r
        FULL OUTER JOIN {G}.user_entitlement e USING (user_principal)
        WHERE r.user_principal IS NULL OR e.user_principal IS NULL""",
    "nobody who has left holds an entitlement": f"""
        SELECT e.user_principal || ' has left but holds an entitlement'
        FROM {G}.user_entitlement e JOIN {LEAVERS} l USING (user_principal)""",
    "a compile has run in the last 24 hours (heartbeat)": f"""
        SELECT 'last compile: ' || coalesce(CAST(max(run_at) AS STRING), 'never') FROM {G}.compile_log
        WHERE step = 'entitlements' HAVING max(run_at) IS NULL OR max(run_at) < current_timestamp() - INTERVAL 24 HOURS""",
}


def behind(q):
    """Export tables changed more than GRACE_MINUTES ago that the last entitlements or views run did not read."""
    out = []
    used = {}
    for step, vs in q(f"""SELECT step, to_json(max_by(source_versions, run_at)) FROM {G}.compile_log
                          WHERE step IN ('entitlements', 'views') GROUP BY step"""):
        import json
        used[step] = json.loads(vs) if vs else {}
    for t in SOURCES:
        v, age = q(f"""SELECT version, (unix_timestamp(current_timestamp()) - unix_timestamp(timestamp)) / 60
                       FROM (DESCRIBE HISTORY {X}.{t}) WHERE operation NOT IN ({", ".join(f"'{o}'" for o in config.MAINTENANCE_OPS)})
                       ORDER BY version DESC LIMIT 1""")[0]
        step = "views" if t == "column_classification" else "entitlements"
        got = used.get(step, {}).get(t)
        if (got is None or int(v) > int(got)) and float(age) > GRACE_MINUTES:
            out.append([f"{t} v{v} ({float(age):.0f} min old) not compiled by '{step}' (used v{got})"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="who", default=None)
    a = ap.parse_args()
    w = dbsql.client(a.who)

    def q(sql):
        return dbsql.run(w, sql, quiet=True)[1]
    failed = 0
    checks = dict(CHECKS)
    checks[f"every policy change is compiled within {GRACE_MINUTES} minutes"] = behind
    for name, check in checks.items():
        bad = check(q) if callable(check) else q(check)
        failed += bool(bad)
        print(f"{'PASS' if not bad else 'FAIL'}  {name}" + (f"  -> {[r[0] for r in bad][:10]}" if bad else ""))
    print(f"\n{failed} invariant(s) violated")
    if failed:  # raise, not sys.exit: a serverless task treats any SystemExit as a failure
        raise RuntimeError(f"{failed} governance invariant(s) violated")
