"""Exfiltration tests: each one tries to get more than the policies allow, through the SQL warehouse, as a test user.
Every check must be refused, or return only what the user may see.
Run: python3 -I tests/test_security.py"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config  # noqa: E402
import dbsql  # noqa: E402

C, B, S, X, G = config.CATALOG, f"{config.CATALOG}.{config.BASE}", f"{config.CATALOG}.{config.SECURE}", \
    f"{config.CATALOG}.{config.EXPORTS}", f"{config.CATALOG}.{config.GOV}"
A = f"{config.CATALOG}.{config.AUDIT}"
V = f"{S}.associate_compensation{config.VIEW_SUFFIX}"
MM = "mm_market_abc"  # Market Manager for Markets A-C (story rows 50001-50003), associate 50001 with Self

# (name, who, SQL, expected): expected is "denied" (the statement must fail), "admin-only" (must fail, except on a
# metastore that `account users` own: there every principal is a metastore admin, see below), or the exact first value
CASES = [
    ("read a base table directly", MM, f"SELECT count(*) FROM {B}.associate_compensation", "denied"),
    ("describe a base table", MM, f"DESCRIBE TABLE {B}.associate_compensation", "admin-only"),
    ("read the policy exports", MM, f"SELECT count(*) FROM {X}.assoc_roles_dim", "denied"),
    ("read the compiled entitlements", MM, f"SELECT count(*) FROM {G}.user_entitlement", "denied"),
    ("read the associates' scope table", MM, f"SELECT count(*) FROM {G}.assoc_scope", "denied"),
    ("read the associates' scope node table (scope_check: join)", MM, f"SELECT count(*) FROM {G}.assoc_scope_node", "denied"),
    ("list the restricted tables in information_schema", MM,
     f"SELECT count(*) FROM {C}.information_schema.tables WHERE table_schema IN ('{config.BASE}', '{config.EXPORTS}', '{config.GOV}')", "admin-only"),
    ("replace a secured view with an unfiltered one", MM,
     f"CREATE OR REPLACE VIEW {V} AS SELECT * FROM {B}.associate_compensation", "denied"),
    ("create a view of one's own", MM, f"CREATE VIEW {S}.my_copy AS SELECT * FROM {V}", "denied"),
    ("drop a secured view", MM, f"DROP VIEW {V}", "denied"),
    ("copy the view into a table", MM, f"CREATE TABLE {S}.my_copy AS SELECT * FROM {V}", "denied"),
    ("time travel on a secured view", MM, f"SELECT count(*) FROM {V} VERSION AS OF 0", "denied"),
    ("grant oneself the base tables", MM, f"GRANT USE SCHEMA, SELECT ON SCHEMA {B} TO `{{{{SP:{MM}}}}}`", "admin-only"),
    # the audit layer: only the admin and the auditors read it, nobody can change it
    ("read the audit views (not an auditor)", MM, f"SELECT count(*) FROM {A}.view_reads WHERE event_date >= current_date()", "denied"),
    ("read the entitlement history through the audit schema (not an auditor)", MM,
     f"SELECT count(*) FROM {A}.entitlement_changes", "denied"),
    ("create a view in the audit schema", MM, f"CREATE VIEW {A}.my_copy AS SELECT 1 AS x", "denied"),
    # inference through predicates, aggregates and joins on values the user may not see
    ("guess a hidden salary with a filter (Maria's 97000, Market D)", MM,
     f"SELECT count(*) FROM {V} WHERE base_salary = 97000", "0"),
    ("filter on a hidden SSN", MM, f"SELECT count(*) FROM {V} WHERE ssn = '900-10-0004'", "0"),
    ("sum the bonus over every row (only Markets A-C count: 16000+13000+11000)", MM, f"SELECT sum(bonus) FROM {V}", "40000"),
    ("largest salary over every row (only the visible ones count)", MM, f"SELECT max(base_salary) FROM {V}", "131000"),
    ("join the view to itself on the SSN (only the own SSN is visible)", MM,
     f"SELECT count(*) FROM {V} a JOIN {V} b ON a.ssn = b.ssn", "1"),
    ("count distinct SSNs (only the own one)", MM, f"SELECT count(DISTINCT ssn) FROM {V}", "1"),
    ("sort by a hidden column to rank rows (hidden values sort as NULL)", MM,
     f"SELECT win FROM {V} WHERE win IN (50004, 50005, 50006) ORDER BY base_salary DESC NULLS LAST, win LIMIT 1", "50004"),
    ("the same SQL as the People Analyst gives the People Analyst's answer, not a cached one", "people_analyst",
     f"SELECT sum(bonus) FROM {V}", None),
    # who sees rows at all
    ("a user with no persona sees no rows", "no_persona", f"SELECT count(*) FROM {V}", "0"),
    ("a user with only Self sees only their own row", "self_only", f"SELECT count(*) || ':' || max(win) FROM {V}", "1:50004"),
    ("a user with no persona reads no attrition rows", "no_persona", f"SELECT count(*) FROM {S}.associate_attrition{config.VIEW_SUFFIX}", "0"),
    # a filter that fails on a hidden row would reveal that row if it ran before the view's own checks
    ("a filter that divides by zero on a hidden row (Priya Shah) answers normally (only Self)", "self_only",
     f"SELECT count(*) FROM {V} WHERE 1 / (CASE WHEN employee_name = 'Priya Shah' THEN 0 ELSE 1 END) > 0", "1"),
    ("raise_error in a filter on a hidden row answers normally (no persona)", "no_persona",
     f"SELECT count(*) FROM {V} WHERE CASE WHEN employee_name = 'Priya Shah' THEN raise_error('found') ELSE true END", "0"),
]


def main():
    owner = dbsql.client().metastores.summary().owner
    everyone_admin = owner == "account users"
    if everyone_admin:
        print("NOTE  this metastore is owned by `account users`: every principal is a metastore admin and can read metadata"
              " and grant itself privileges. Production: a small admin group owns the metastore, so these are refused.\n")
    failures = 0
    for name, who, sql, expected in CASES:
        u = dbsql.client(who)
        msg = ""
        try:
            rows = dbsql.run(u, sql, quiet=True)[1]
            got = "NULL" if not rows or rows[0][0] is None else str(rows[0][0])
        except RuntimeError as e:
            got, msg = "denied", str(e).splitlines()[0][:90]
        if expected == "admin-only":
            ok = got == "denied" or (got == "0" and "information_schema" in sql)
            note = ""
            if not ok and everyone_admin:
                ok, note = True, "  [allowed only because every principal is a metastore admin here]"
                if sql.startswith("GRANT"):  # it worked: prove the access is real, then that the guard takes it away
                    try:
                        dbsql.run(u, f"SELECT count(*) FROM {B}.associate_compensation", quiet=True)
                    except RuntimeError:
                        print("      (the self-grant did not give read access)")
                    subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(__file__), "..", "compiler",
                                    "fgac_compiler.py"), "guard", "--as", "compiler"], check=True,
                                   stdout=subprocess.DEVNULL)
                    try:
                        dbsql.run(u, f"SELECT count(*) FROM {B}.associate_compensation", quiet=True)
                        ok, note = False, "  [the guard did NOT remove the self-granted access]"
                    except RuntimeError:
                        note = "  [the guard step revoked it: reading the base table is denied again]"
            print(f"{'PASS' if ok else 'FAIL'}  {who:15} {name}: {got}{note}")
        else:
            ok = got == (expected if expected is not None else "NULL")
            print(f"{'PASS' if ok else 'FAIL'}  {who:15} {name}: {got}" + (f" ({msg})" if msg else "")
                  + ("" if ok else f"   EXPECTED {expected}"))
        failures += not ok
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
