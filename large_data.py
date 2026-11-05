"""The optional large synthetic data set: the same compiler and secured views on many more associates, personas, users
and columns. It lives in its own catalog (config.json "large_data"), so the POC catalog and the step-by-step guide are
untouched, and one command removes it. Deploy the POC first (python3 -I poc.py deploy): this reuses its compiler service
principal and its test users, with new assignments in the large catalog.

  python3 -I large_data.py load [--associates N]   create the build warehouse and the catalog, generate the data, grant,
                                                    compile (as the compiler service principal)
  python3 -I large_data.py status                   what exists, and the size of each table
  python3 -I large_data.py act-as <persona>|none    your login holds that test user's assignment in the large catalog
                                                    (then compile); in the SQL editor you see what that persona sees
  python3 -I large_data.py compile                  compile the large catalog again (after you change its exports)
  python3 -I large_data.py users                    give the test users their assignments again (after a POC redeploy)
  python3 -I large_data.py remove --yes             drop the large catalog and delete the build warehouse

What it generates (N associates, default 10,000,000; set large_data.associates in config.json or --associates):
  policy.assoc_dim                 N associates: 5,000 markets, 50 regions, 300 departments, a 5-way reporting tree;
                                   22% Active, the rest Terminated
  policy.persona                   205 personas (Market, Region, Department, Enterprise, Reporting, Self)
  policy.persona_column_policy     4 classifications x 30 categories; each synthetic persona grants 5-40 pairs
  policy.assoc_roles_dim           N/40 users, all with Self; one in five holds 5-20 personas, the first 50 hold 25-30
  policy.column_classification     every classified column of the three tables, plus rows for other tables (skipped)
  hr_base.comp_history             6 rows per associate x 30 columns (compensation history)
  hr_base.associate_profile        1 row per associate x 500 columns (4 named, 496 attributes, a fifth of them classified)
  hr_base.comp_history_wide        6 rows per associate x 500 columns
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402  (first, only to read the large-data settings)

LARGE = config.LARGE
os.environ["POC_CATALOG"] = LARGE["catalog"]           # config, dbsql and poc then work on the large catalog
os.environ["POC_WAREHOUSE"] = LARGE["warehouse_name"]  # every statement here runs on the build warehouse
import importlib  # noqa: E402
config = importlib.reload(config)
import dbsql  # noqa: E402
import poc  # noqa: E402  (its grant function, on the large catalog)
from databricks.sdk.service import sql as sqlsvc  # noqa: E402

C, B, X, G, S = config.CATALOG, config.BASE, config.EXPORTS, config.GOV, config.SECURE
assert C != config.CFG["catalog"], "large_data.catalog must differ from the POC catalog"
admin = dbsql.client()

# ------------------------------------------------------------------------------------------- the synthetic world
CLASSES = ["Non-Sensitive", "Sensitive", "Highly Sensitive", "Highly Sensitive Restricted"]
CATEGORIES = ["Compensation", "Contact", "Employment", "Payroll & Tax", "Personal Identifiable", "Organization",
              "Demographic", "Benefits", "Health", "Performance", "Learning", "Recruiting", "Time & Attendance", "Leave",
              "Relocation", "Immigration", "Background Check", "Equity", "Pension", "Succession", "Disciplinary",
              "Grievance", "Safety", "Travel", "Expense", "Union", "Diversity", "Talent", "Workforce Planning", "Contractor"]
MKT = "concat('M', lpad(CAST(id % 5000 AS STRING), 4, '0'))"   # 5,000 markets
REG = "concat('R', lpad(CAST(id % 50 AS STRING), 2, '0'))"     # 50 regions
DEP = "concat('D', lpad(CAST(id % 300 AS STRING), 3, '0'))"    # 300 departments
ASSOC_ID_SQL = "CAST(30000000 + id AS BIGINT)"                           # associate 30000000 + id; 30000000 is the top of the tree
MGR_WIN = 30000031  # the test user people_mgr manages this node, three levels below the top of the reporting tree


def sizes(n):
    """Everything scales with the number of associates."""
    users = max(n // 40, 100)
    return {"associates": n, "active": max(n * 22 // 100, 100), "users": users, "multi": max(users // 5, 50)}


def comp_columns(active):
    """comp_history: 30 columns, (name, SQL over id and r, classification, category, business term)."""
    return [
        ("win", ASSOC_ID_SQL, "Non-Sensitive", "Organization", "Associate ID"),
        ("year", "2021 + CAST(r % 6 AS INT)", "Non-Sensitive", "Organization", "Plan Year"),
        ("effective_date", "make_date(2021 + CAST(r % 6 AS INT), 1 + CAST(id % 12 AS INT), 1)", "Non-Sensitive", "Organization", "Effective Date"),
        ("employee_name", "concat('Associate ', id)", "Non-Sensitive", "Personal Identifiable", "Employee Name"),
        ("email_address", "concat('a', id, '@example.com')", "Non-Sensitive", "Contact", "Email Address"),
        ("market", MKT, "Non-Sensitive", "Organization", "Market"),
        ("region", REG, "Non-Sensitive", "Organization", "Region"),
        ("department", DEP, "Non-Sensitive", "Organization", "Department"),
        ("manager_win", "CAST(30000000 + CASE WHEN id = 0 THEN 0 ELSE (id - 1) DIV 5 END AS BIGINT)", "Non-Sensitive", "Organization", "Manager ID"),
        ("job_code", "concat('J', CAST(id % 400 AS STRING))", "Sensitive", "Employment", "Job Code"),
        ("job_title", "concat('Title ', CAST(id % 900 AS STRING))", "Sensitive", "Employment", "Job Title"),
        ("grade", "CAST(id % 20 AS INT)", "Sensitive", "Employment", "Pay Grade"),
        ("hire_dt", "date_add(DATE'2000-01-01', CAST(id % 9000 AS INT))", "Sensitive", "Employment", "Hire Date"),
        ("base_salary", "CAST(30000 + abs(hash(id, r, 7919)) % 170000 AS INT)", "Sensitive", "Compensation", "Base Salary"),
        ("phone_number", "concat(CAST(id % 900 + 100 AS STRING), '-', lpad(CAST(id % 10000 AS STRING), 4, '0'))", "Sensitive", "Contact", "Phone Number"),
        ("home_city", "concat('City ', CAST(id % 2000 AS STRING))", "Sensitive", "Contact", "Home City"),
        ("bonus", "CAST(abs(hash(id, r, 104729)) % 40000 AS INT)", "Highly Sensitive", "Compensation", "Bonus"),
        ("merit_increase_pct", "CAST(abs(hash(id, r, 1)) % 80 AS DOUBLE) / 10", "Highly Sensitive", "Compensation", "Merit Increase"),
        ("equity_grant", "CAST(abs(hash(id, r, 31)) % 100000 AS INT)", "Highly Sensitive", "Equity", "Equity Grant"),
        ("overtime_pay", "CAST(abs(hash(id, r, 37)) % 9000 AS INT)", "Highly Sensitive", "Compensation", "Overtime Pay"),
        ("commission", "CAST(abs(hash(id, r, 41)) % 15000 AS INT)", "Highly Sensitive", "Compensation", "Commission"),
        ("allowance", "CAST(abs(hash(id, r, 43)) % 3000 AS INT)", "Highly Sensitive", "Benefits", "Allowance"),
        ("term_dt", f"CASE WHEN id >= {active} THEN date_add(DATE'2020-01-01', CAST(id % 2400 AS INT)) END", "Highly Sensitive", "Employment", "Termination Date"),
        ("rating", "element_at(array('Exceeds','Meets','Developing'), CAST(id % 3 + 1 AS INT))", "Highly Sensitive", "Performance", "Performance Rating"),
        ("tax_withholding", "CAST(abs(hash(id, r, 47)) % 9000 AS INT)", "Highly Sensitive", "Payroll & Tax", "Tax Withholding"),
        ("bank_routing", "lpad(CAST(id % 1000000000 AS STRING), 9, '0')", "Highly Sensitive", "Payroll & Tax", "Bank Routing Number"),
        ("ssn", "concat(CAST(id % 900 + 100 AS STRING), '-', CAST(id % 90 + 10 AS STRING), '-', lpad(CAST(id % 10000 AS STRING), 4, '0'))", "Highly Sensitive Restricted", "Personal Identifiable", "SSN"),
        ("birth_dt", "date_add(DATE'1960-01-01', CAST(id % 15000 AS INT))", "Highly Sensitive Restricted", "Personal Identifiable", "Birth Date"),
        ("gender", "element_at(array('F','M','X'), CAST(id % 3 + 1 AS INT))", "Highly Sensitive Restricted", "Demographic", "Gender"),
        ("ethnicity", "concat('E', CAST(id % 7 AS STRING))", "Highly Sensitive Restricted", "Demographic", "Ethnicity"),
    ]


def attrs(first, last):
    """attr_<n> columns: every 4th a string, the rest integers; every 5th classified (round-robin over classes and
    categories), the others unclassified (the unclassified_rule applies)."""
    out = []
    for n in range(first, last + 1):
        p = 1000003 + 2 * n
        expr = (f"concat('v', CAST((id * {p} + r) % 997 AS STRING))" if n % 4 == 0
                else f"CAST((id * {p} + r * {n}) % 100003 AS INT)")
        cls, cat = (CLASSES[(n // 5) % 4], CATEGORIES[(n // 5) % 30]) if n % 5 == 0 else (None, None)
        out.append((f"attr_{n:03d}", expr, cls, cat, f"Attribute {n}" if cls else None))
    return out


def tables(z):
    comp = comp_columns(z["active"])
    profile = [c for c in comp if c[0] in ("win", "market", "region", "department")] + attrs(5, 500)
    wide = comp + attrs(31, 500)
    assert len(comp) == 30 and len(profile) == 500 and len(wide) == 500
    n = z["associates"]
    return {"comp_history": (comp, 6 * n, 6), "associate_profile": (profile, n, 1), "comp_history_wide": (wide, 6 * n, 6)}


def table_sql(name, spec):
    cols, rows, per = spec
    sel = ",\n  ".join(f"{e} AS `{c}`" for c, e, *_ in cols)
    return (f"CREATE OR REPLACE TABLE {C}.{B}.{name} AS\nSELECT\n  {sel}\n"
            f"FROM (SELECT r, r DIV {per} AS id FROM range(0, {rows}) AS t(r))")


def classification_sql(tbls):
    rows = []
    for t, (cols, *_) in tbls.items():
        rows += [(f"{C}.{B}.{t}.{c}", term, cls, cat) for c, _, cls, cat, term in cols if cls]
    k = 0
    while len(rows) < 6000:  # the data catalog covers more than these tables: rows the compiler must skip
        rows.append((f"{C}.other_domain.table_{k // 40:03d}.col_{k % 40:02d}", f"Business term {k}",
                     CLASSES[k % 4], CATEGORIES[k % 30]))
        k += 1
    vals = ",\n".join("(" + ", ".join("'" + v.replace("'", "''") + "'" for v in r) + ")" for r in rows)
    return [f"""CREATE OR REPLACE TABLE {C}.{X}.column_classification (
  qualified_column_name STRING, business_term STRING, classification STRING, data_security_category STRING)""",
            f"INSERT INTO {C}.{X}.column_classification VALUES\n{vals}"]


def test_user_rows():
    """The POC's test users, with assignments in the large catalog (user, win, persona, scope value)."""
    pa, rh, mm, mg, mu, so = (config.app_id(k) for k in ("people_analyst", "rhrd_south", "mm_market_abc",
                                                          "people_mgr", "multi_persona", "self_only"))
    r = [(pa, 30000002, "ANALYST", None), (rh, 30000003, "RHRD", "R07"),
         (mm, 30000001, "SELF", None)] + [(mm, 30000001, "MM", f"M{m:04d}") for m in (1, 2, 3)] + \
        [(mg, MGR_WIN, "SELF", None), (mg, MGR_WIN, "MGR", str(MGR_WIN)), (so, 30000004, "SELF", None)]
    for i in range(1, 31):  # multi_persona: 30 synthetic personas, scope values by each persona's type
        pid, typ = f"P{i:03d}", ["Market", "Region", "Department", "Enterprise", "Reporting"][i % 5]
        vals = {"Market": [f"M{(100 + 20 * i + j) % 5000:04d}" for j in range(20)], "Region": [f"R{(i + j) % 50:02d}" for j in range(2)],
                "Department": [f"D{(10 * i + j) % 300:03d}" for j in range(10)], "Enterprise": [None],
                "Reporting": [str(30000000 + 6 + i), str(30000000 + 40 + i)]}[typ]
        r += [(mu, 30000005, pid, v) for v in vals]
    return r


def test_user_insert():
    vals = ",\n".join(f"('{u}', {w}, '{p}', {'NULL' if v is None else repr(v)}, current_timestamp())"
                      for u, w, p, v in test_user_rows())
    return f"INSERT INTO {C}.{X}.assoc_roles_dim VALUES\n{vals}"


def exports_sql(z):
    n, active, users, multi = z["associates"], z["active"], z["users"], z["multi"]
    q = [f"""CREATE OR REPLACE TABLE {C}.{X}.assoc_dim AS
SELECT {ASSOC_ID_SQL} AS win, concat('Associate ', id) AS employee_name, {MKT} AS market, {REG} AS region, {DEP} AS department,
       transform(filter(sequence(1, 12), j -> id >= (CAST(pow(5, j) AS BIGINT) - 1) DIV 4),
                 j -> CAST(30000000 + (id - (CAST(pow(5, j) AS BIGINT) - 1) DIV 4) DIV CAST(pow(5, j) AS BIGINT) AS BIGINT)) AS reporting_chain,
       CASE WHEN id < {active} THEN 'Active' ELSE 'Terminated' END AS status
FROM range(0, {n}) AS t(id)""",
         f"""CREATE OR REPLACE TABLE {C}.{X}.persona AS
SELECT * FROM VALUES ('ANALYST', 'People Analyst', 'Enterprise'), ('RHRD', 'Regional HR Director', 'Region'),
  ('MM', 'Market Manager', 'Market'), ('MGR', 'People Manager', 'Reporting'), ('SELF', 'Self', 'Self')
  AS t(persona_id, persona_name, scope_type)
UNION ALL
SELECT concat('P', lpad(CAST(id AS STRING), 3, '0')), concat('Synthetic persona ', id),
       element_at(array('Market','Region','Department','Enterprise','Reporting'), CAST(id % 5 + 1 AS INT))
FROM range(1, 201) AS t(id)"""]
    pairs = ", ".join(f"('{c}', '{g}')" for c in CLASSES[1:] for g in CATEGORIES)
    named = {"ANALYST": [("Sensitive", "Compensation"), ("Sensitive", "Employment"), ("Sensitive", "Contact")],
             "RHRD": [("Sensitive", "Compensation"), ("Sensitive", "Employment"), ("Sensitive", "Contact"),
                      ("Highly Sensitive", "Compensation"), ("Highly Sensitive", "Payroll & Tax"), ("Highly Sensitive", "Employment")],
             "MM": [("Sensitive", "Compensation"), ("Sensitive", "Employment"), ("Sensitive", "Contact"),
                    ("Highly Sensitive", "Compensation"), ("Highly Sensitive", "Payroll & Tax")],
             "MGR": [("Sensitive", "Compensation"), ("Highly Sensitive", "Compensation"), ("Sensitive", "Employment"),
                     ("Sensitive", "Contact")]}
    nv = ", ".join(f"('{p}', '{c}', '{g}')" for p, lst in named.items() for c, g in lst)
    q.append(f"""CREATE OR REPLACE TABLE {C}.{X}.persona_column_policy AS
SELECT * FROM VALUES {nv} AS t(persona_id, classification, data_security_category)
UNION ALL SELECT 'SELF', c, g FROM VALUES {pairs} AS v(c, g)
UNION ALL
SELECT persona_id, c, g FROM (
  SELECT p.persona_id, v.c, v.g, row_number() OVER (PARTITION BY p.persona_id ORDER BY abs(hash(p.persona_id, v.c, v.g))) AS k,
         5 + abs(hash(p.persona_id)) % 36 AS n
  FROM {C}.{X}.persona p CROSS JOIN VALUES {pairs} AS v(c, g) WHERE p.persona_id RLIKE '^P[0-9]{{3}}$')
WHERE k <= n""")
    q.append(f"""CREATE OR REPLACE TABLE {C}.{X}.assoc_roles_dim
  (user_principal STRING, win BIGINT, persona_id STRING, scope_value STRING, updated_at TIMESTAMP)""")
    # every user has Self; one in five holds 5-20 personas (the first 50: 25-30), the others one
    q.append(f"""INSERT INTO {C}.{X}.assoc_roles_dim
SELECT concat('u', u, '@example.com'), CAST(30000000 + (u * 7) % {active} AS BIGINT), 'SELF', NULL, current_timestamp()
FROM range(1, {users + 1}) AS t(u)""")
    q.append(f"""INSERT INTO {C}.{X}.assoc_roles_dim
SELECT concat('u', a.u, '@example.com'), CAST(30000000 + (a.u * 7) % {active} AS BIGINT), p.persona_id,
       CASE p.scope_type
         WHEN 'Market' THEN concat('M', lpad(CAST(abs(hash(a.u, a.i, j)) % 5000 AS STRING), 4, '0'))
         WHEN 'Region' THEN concat('R', lpad(CAST(abs(hash(a.u, a.i, j)) % 50 AS STRING), 2, '0'))
         WHEN 'Department' THEN concat('D', lpad(CAST(abs(hash(a.u, a.i, j)) % 300 AS STRING), 3, '0'))
         WHEN 'Reporting' THEN CAST(30000000 + abs(hash(a.u, a.i, j)) % {min(n, 400000)} AS STRING)
       END, current_timestamp()
FROM (SELECT u, explode(sequence(1, CASE WHEN u <= 50 THEN 25 + CAST(u % 6 AS INT)
                                         WHEN u <= {multi} THEN 5 + CAST(u % 16 AS INT) ELSE 1 END)) AS i
      FROM range(1, {users + 1}) AS t(u)) a
JOIN {C}.{X}.persona p ON p.persona_id = concat('P', lpad(CAST(abs(hash(a.u, a.i)) % 200 + 1 AS STRING), 3, '0'))
LATERAL VIEW explode(sequence(1, CASE p.scope_type WHEN 'Market' THEN 1 + CAST(abs(hash(a.u, a.i)) % 8 AS INT)
                                                   WHEN 'Region' THEN 1 + CAST(abs(hash(a.u, a.i)) % 2 AS INT)
                                                   WHEN 'Department' THEN 1 + CAST(abs(hash(a.u, a.i)) % 3 AS INT)
                                                   ELSE 1 END)) s AS j""")
    q.append(test_user_insert())
    return q


# ------------------------------------------------------------------------------------------- commands
def q1(sql):
    return dbsql.run(admin, sql, quiet=True)[1]


def warehouse():
    return next((x for x in admin.warehouses.list() if x.name == LARGE["warehouse_name"]), None)


def ensure_warehouse():
    """The build warehouse: the compiler and the test users may use it; the POC warehouses work on this catalog too."""
    wh = warehouse()
    if not wh:
        admin.warehouses.create_and_wait(name=LARGE["warehouse_name"], cluster_size=LARGE["warehouse_size"],
                                         min_num_clusters=1, max_num_clusters=1, auto_stop_mins=10,
                                         enable_serverless_compute=True,
                                         warehouse_type=sqlsvc.CreateWarehouseRequestWarehouseType.PRO)
        wh = warehouse()
        print(f"created the warehouse {wh.name} (serverless, {LARGE['warehouse_size']}, stops after 10 idle minutes)")
    admin.warehouses.update_permissions(wh.id, access_control_list=[
        sqlsvc.WarehouseAccessControlRequest(service_principal_name=c["application_id"],
                                             permission_level=sqlsvc.WarehousePermissionLevel.CAN_USE)
        for c in config.creds().values()])


def step(label, sql):
    print(f"  {label}")
    dbsql.run(admin, sql, quiet=True)


def compile_all():
    print(f"compile {C}, as the compiler service principal ...")
    p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "compiler", "fgac_compiler.py"), "all",
                        "--as", config.COMPILER], env=os.environ.copy())
    if p.returncode != 0:
        sys.exit("compile FAILED (see the output above)")


def need_poc():
    if not config.app_id(config.COMPILER):
        sys.exit(f"the POC is not deployed (no compiler secret in {config.SP_CREDS}): python3 -I poc.py deploy first")


def load(a):
    need_poc()
    z = sizes(a.associates or int(LARGE["associates"]))
    tbls = tables(z)
    ensure_warehouse()
    print(f"catalog {C}: {z['associates']:,} associates, {z['users']:,} users (synthetic)")
    step("catalog and schemas", f"CREATE CATALOG IF NOT EXISTS {C} COMMENT 'FGAC large synthetic data set'")
    for s in (B, X, G, S):
        dbsql.run(admin, f"CREATE SCHEMA IF NOT EXISTS {C}.{s}", quiet=True)
    for sql in exports_sql(z) + classification_sql(tbls):
        step(sql.split("\n")[0][:100], sql)
    for t, spec in tbls.items():
        step(f"{t}: {spec[1]:,} rows x {len(spec[0])} columns", table_sql(t, spec))
    poc.ensure_grants()  # the same ownership and grants as the POC catalog
    ensure_warehouse()
    compile_all()
    print(f"\nDone. In the SQL editor: {C}.{S}.comp_history_vw, associate_profile_vw, comp_history_wide_vw. "
          "Then: python3 -I large_data.py act-as <persona>")


def status(a):
    wh = warehouse()
    print(f"warehouse  {LARGE['warehouse_name']}: {'present' if wh else 'absent'}")
    if not any(c.name == C for c in admin.catalogs.list()):
        print(f"catalog    {C}: absent")
        return
    print(f"catalog    {C}: present")
    if not wh:
        return
    for s, t in [(X, "assoc_dim"), (X, "assoc_roles_dim"), (B, "comp_history"), (B, "associate_profile"), (B, "comp_history_wide")]:
        try:
            print(f"  {s}.{t}: {int(q1(f'SELECT count(*) FROM {C}.{s}.{t}')[0][0]):,} rows")
        except RuntimeError:
            print(f"  {s}.{t}: absent")
    mine = q1(f"SELECT array_join(collect_set(persona_id), ', ') FROM {C}.{X}.assoc_roles_dim WHERE user_principal = '{config.ADMIN}'")
    print(f"your login {config.ADMIN}: personas here = {mine[0][0] or 'none'}")


def act_as(a):
    me, who = config.ADMIN, a.persona
    if who not in ("none", *config.PERSONA_KEYS):
        sys.exit(f"choose one of {', '.join(config.PERSONA_KEYS)} or none")
    if who not in ("none", "no_persona") and not config.app_id(who):
        sys.exit(f"{who} has no secret in {config.SP_CREDS}: python3 -I poc.py deploy first")
    if who in ("hrbp_hr", "senior_mgr"):
        print(f"note: {who} has no assignment in the large data set: your login gets nothing (as for none)")
    q1(f"DELETE FROM {C}.{X}.assoc_roles_dim WHERE user_principal = '{me}'")
    if who not in ("none", "no_persona"):
        q1(f"""INSERT INTO {C}.{X}.assoc_roles_dim SELECT '{me}', win, persona_id, scope_value, current_timestamp()
               FROM {C}.{X}.assoc_roles_dim WHERE user_principal = '{config.app_id(who)}'""")
    compile_all()
    print(f"Done: in the SQL editor, {me} now sees what {who} sees in {C}.{S}. Back: python3 -I large_data.py act-as none")


def users(a):
    """Give the POC's test users their assignments again (after the POC was redeployed: new application IDs)."""
    q1(f"DELETE FROM {C}.{X}.assoc_roles_dim WHERE user_principal NOT LIKE 'u%@example.com' AND user_principal <> '{config.ADMIN}'")
    q1(test_user_insert())
    ensure_warehouse()
    compile_all()


def remove(a):
    if not a.yes:
        sys.exit("drops the large catalog and deletes its warehouse: run again with --yes")
    if any(c.name == C for c in admin.catalogs.list()):
        if warehouse():
            q1(f"DROP CATALOG {C} CASCADE")
        else:  # the build warehouse is gone: drop it on the POC warehouse
            os.environ["POC_WAREHOUSE"] = config.WAREHOUSE_NAME
            dbsql._wh.clear()
            q1(f"DROP CATALOG {C} CASCADE")
        print(f"dropped {C}")
    wh = warehouse()
    if wh:
        admin.warehouses.delete(wh.id)
        print(f"deleted the warehouse {wh.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("--associates", type=int, default=None)
    sub.add_parser("status")
    p = sub.add_parser("act-as")
    p.add_argument("persona")
    sub.add_parser("compile")
    sub.add_parser("users")
    p = sub.add_parser("remove")
    p.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    {"load": load, "status": status, "act-as": act_as, "compile": lambda a: compile_all(), "users": users,
     "remove": remove}[a.cmd](a)
