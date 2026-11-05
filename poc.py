"""Build, check and remove the POC with one command each. Every value comes from config.json.
  python3 -I poc.py deploy [--verify]     build everything that is missing (idempotent); --verify also runs the
                                          compiler job once and the test suites
  python3 -I poc.py status                what exists, and which persona your own login holds
  python3 -I poc.py destroy --yes         remove everything the deploy created: jobs, catalog, principals (in the
                                          account), the SQL warehouses and the local secrets file; also the large
                                          data set (large_data.py) if it was loaded
  python3 -I poc.py act-as <persona>      give your own login a test persona's policy rows, then wait for the
                                          compiler; your SQL editor then shows exactly what that persona sees
  python3 -I poc.py act-as none           remove your login's policy rows again (you see nothing in the views)
  python3 -I poc.py run-job [compiler|guardian]   run a job once and wait for it"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import dbsql  # noqa: E402
from databricks.sdk import AccountClient, WorkspaceClient  # noqa: E402
from databricks.sdk.service import iam, sql as sqlsvc  # noqa: E402

w = WorkspaceClient(profile=config.PROFILE)
C, X = config.CATALOG, f"{config.CATALOG}.{config.EXPORTS}"
KEYS = config.PERSONA_KEYS + [config.COMPILER]
JOBS = {"compiler": f"{config.JOB_PREFIX}compiler", "guardian": f"{config.JOB_PREFIX}guardian"}


def q(sql):
    return dbsql.run(w, sql, quiet=True)[1]


def step(title):
    print(f"\n=== {title}")


# ------------------------------------------------------------------------------------------------ pieces
def warehouse(name=config.WAREHOUSE_NAME):
    return next((x for x in w.warehouses.list() if x.name == name), None) if name else None


def ensure_warehouse():
    step(f"SQL warehouse {config.WAREHOUSE_NAME}")
    if warehouse():
        print("    exists")
        return
    w.warehouses.create_and_wait(name=config.WAREHOUSE_NAME, cluster_size="Small", min_num_clusters=1,
                                 max_num_clusters=1, auto_stop_mins=15, enable_serverless_compute=True,
                                 warehouse_type=sqlsvc.CreateWarehouseRequestWarehouseType.PRO)
    print("    created (serverless, Small, stops after 15 idle minutes)")


def ensure_rt_warehouse():
    """Lakehouse Real-Time (Beta), for the users' read queries. Optional: skipped when config.json has no
    realtime_warehouse_name, or when the workspace's "Lakehouse RT" preview is off."""
    if not config.RT_WAREHOUSE_NAME:
        return
    step(f"Lakehouse Real-Time warehouse {config.RT_WAREHOUSE_NAME}")
    if warehouse(config.RT_WAREHOUSE_NAME):
        print("    exists")
    else:
        try:
            w.warehouses.create(name=config.RT_WAREHOUSE_NAME, cluster_size="Small", min_num_clusters=1, max_num_clusters=1,
                                auto_stop_mins=10, enable_serverless_compute=True,
                                warehouse_type=sqlsvc.CreateWarehouseRequestWarehouseType.REALTIME)
            print("    created (Real-Time, Small, stops after 10 idle minutes)")
        except Exception as e:  # noqa: BLE001 - the Beta is off in this workspace: everything else still works
            print(f"    not created ({str(e)[:160]}): turn on the Lakehouse RT preview (workspace menu > Previews)")
            return
    creds = config.creds()
    w.warehouses.update_permissions(warehouse(config.RT_WAREHOUSE_NAME).id, access_control_list=[
        sqlsvc.WarehouseAccessControlRequest(service_principal_name=creds[k]["application_id"],
                                             permission_level=sqlsvc.WarehousePermissionLevel.CAN_USE)
        for k in config.PERSONA_KEYS if k in creds])
    print("    CAN_USE for the test users")


def find_sp(name):
    return next(iter(w.service_principals.list(filter=f'displayName eq "{name}"')), None)


def ensure_principals():
    step("service principals (the test users and the compiler) and their secrets")
    creds = config.creds()
    for key in KEYS:
        name = config.principal_name(key)
        sp = find_sp(name)
        if sp is None:
            sp = w.service_principals.create(display_name=name, active=True, entitlements=[
                iam.ComplexValue(value="workspace-access"), iam.ComplexValue(value="databricks-sql-access")])
            print(f"    created {name}")
        if creds.get(key, {}).get("application_id") != sp.application_id:
            secret = w.service_principal_secrets_proxy.create(service_principal_id=sp.id)
            creds[key] = {"application_id": sp.application_id, "id": sp.id, "secret": secret.secret}
            print(f"    new secret for {name}")
    fd = os.open(config.SP_CREDS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # also when the file already existed with looser permissions: before any secret is written
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(config.SP_CREDS, 0o600)
    wh = warehouse()
    w.warehouses.update_permissions(wh.id, access_control_list=[
        sqlsvc.WarehouseAccessControlRequest(service_principal_name=creds[k]["application_id"],
                                             permission_level=sqlsvc.WarehousePermissionLevel.CAN_USE) for k in KEYS])
    print(f"    {len(KEYS)} principals ready; secrets in {config.SP_CREDS}; CAN_USE on the warehouse")


def catalog_exists():
    return any(c.name == C for c in w.catalogs.list())


DATA_TABLES = [(config.BASE, t) for t in ("associate_compensation", "associate_attrition", "market_summary")] + \
    [(config.EXPORTS, t) for t in ("assoc_dim", "persona", "persona_column_policy", "assoc_roles_dim", "column_classification")]


def data_ok():
    """Every base and export table exists and has rows (a deploy that failed halfway is rebuilt)."""
    if not catalog_exists():
        return False
    have = {(s, t) for s, t in q(f"SELECT table_schema, table_name FROM {C}.information_schema.tables")}
    if not all(x in have for x in DATA_TABLES):
        return False
    return all(int(q(f"SELECT count(*) FROM {C}.{s}.{t}")[0][0]) > 0 for s, t in DATA_TABLES)


def ensure_data():
    step(f"catalog {C}: HR base tables and policy exports (synthetic)")
    for f in ("01_catalog_and_hr_data.sql", "02_policy_exports.sql"):
        dbsql.run_file(w, os.path.join(HERE, "setup", f))
        print(f"    ran setup/{f}")


def ensure_grants():
    step("grants and ownership")
    comp = config.app_id(config.COMPILER)
    for s in [f"GRANT ALL PRIVILEGES, MANAGE ON CATALOG {C} TO `{config.ADMIN}`",
              f"ALTER CATALOG {C} OWNER TO `{comp}`",
              # every user may open the catalog and read the views schema; the views decide what they see
              f"GRANT USE CATALOG ON CATALOG {C} TO `account users`",
              f"GRANT USE SCHEMA, SELECT ON SCHEMA {C}.{config.SECURE} TO `account users`",
              # the compiler must read every base table, also tables that others (ETL) create later
              f"GRANT USE SCHEMA, SELECT ON SCHEMA {C}.{config.BASE} TO `{comp}`"]:
        q(s)
    # a deployment from before the audit layer keeps its data (setup not rerun): the audit schema may be missing
    q(f"CREATE SCHEMA IF NOT EXISTS {C}.{config.AUDIT} COMMENT 'Generated audit views over the system tables and the "
      "governance history. The admin and the auditors only.'")
    for s in (config.BASE, config.SECURE, config.EXPORTS, config.GOV, config.AUDIT):
        q(f"ALTER SCHEMA {C}.{s} OWNER TO `{comp}`")
    for t, in q(f"SELECT table_schema || '.' || table_name FROM {C}.information_schema.tables "
                f"WHERE table_schema IN ('{config.BASE}', '{config.EXPORTS}', '{config.GOV}')"):
        q(f"ALTER TABLE {C}.{t} OWNER TO `{comp}`")
    print(f"    the compiler owns the catalog and everything in it; you keep ALL PRIVILEGES + MANAGE;"
          f" `account users` may read {config.SECURE} only")
    # the audit views read the Unity Catalog audit log and the query history with the compiler's rights
    try:
        for s in [f"GRANT USE CATALOG ON CATALOG system TO `{comp}`",
                  f"GRANT USE SCHEMA, SELECT ON SCHEMA system.access TO `{comp}`",
                  f"GRANT USE SCHEMA, SELECT ON SCHEMA system.query TO `{comp}`"]:
            q(s)
        print("    the compiler may read system.access (audit log) and system.query (query history), for the audit views")
    except RuntimeError as e:
        print(f"    WARNING: could not let the compiler read the system tables ({str(e).splitlines()[0][:120]}): a metastore"
              " admin must run these grants, or the audit step fails (Runbook section 6)")


def compile_now():
    """Through the compiler job when it exists: the job runs one at a time (queue), so a compile never overlaps one
    that an export change has started. Before the first job deploy, locally as the compiler."""
    step("compile, as the compiler service principal")
    if job_id("compiler"):
        if result(run_job("compiler")) != "SUCCESS":
            sys.exit("The compiler job FAILED: see Jobs & Pipelines")
        return
    subprocess.run([sys.executable, "-I", os.path.join(HERE, "compiler", "fgac_compiler.py"), "all",
                    "--as", config.COMPILER], check=True)


def allow_deploy_as_compiler():
    """A job may run as a service principal only if its deployer has the Service Principal User role on it."""
    a = AccountClient(profile=config.ACCOUNT_PROFILE)
    name = f"accounts/{config.ACCOUNT_ID}/servicePrincipals/{config.app_id(config.COMPILER)}/ruleSets/default"
    rs = a.access_control.get_rule_set(name=name, etag="")
    rules, me = list(rs.grant_rules or []), f"users/{config.ADMIN}"
    for role in ("roles/servicePrincipal.user", "roles/servicePrincipal.manager"):  # add the admin; keep everyone else
        r = next((x for x in rules if x.role == role), None)
        if r is None:
            rules.append(iam.GrantRule(role=role, principals=[me]))
        elif me not in (r.principals or []):
            r.principals = [*(r.principals or []), me]
    a.access_control.update_rule_set(name=name, rule_set=iam.RuleSetUpdateRequest(name=name, etag=rs.etag, grant_rules=rules))


def compiler_app_id():
    """From the secrets file, or (after a partial destroy) from the workspace."""
    if config.app_id(config.COMPILER):
        return config.app_id(config.COMPILER)
    sp = find_sp(config.principal_name(config.COMPILER))
    return sp.application_id if sp else None


def bundle(*args):
    cid = compiler_app_id()
    if not cid:
        sys.exit("can't find the compiler service principal: the bundle needs its application ID")
    cmd = ["databricks", "bundle", *args, "-p", config.PROFILE, f"--var=compiler_app_id={cid}",
           f"--var=catalog={C}", f"--var=job_prefix={config.JOB_PREFIX}"]
    print("    $", " ".join(cmd))
    return subprocess.run(cmd, cwd=HERE)


def ensure_jobs():
    step(f"jobs (Databricks Asset Bundle): {JOBS['compiler']} and {JOBS['guardian']}")
    if config.CATALOG != config.CFG["catalog"]:  # the job's tasks read config.json, not POC_CATALOG
        sys.exit(f"POC_CATALOG={config.CATALOG} differs from config.json ({config.CFG['catalog']}): the jobs would compile "
                 "config.json's catalog. Change config.json instead.")
    allow_deploy_as_compiler()
    if bundle("deploy").returncode != 0:  # the run-as permission can take a minute to apply
        print("    retrying in 60 seconds")
        time.sleep(60)
        if bundle("deploy").returncode != 0:
            sys.exit("bundle deploy failed")


def result(r):
    """A finished run's result (SUCCESS, FAILED, ...), or its life-cycle state when it has none (for example SKIPPED)."""
    return r.state.result_state.value if r.state.result_state else r.state.life_cycle_state.value


def job_id(key):
    return next((j.job_id for j in w.jobs.list(name=JOBS[key])), None)


def run_job(key):
    jid = job_id(key)
    if not jid:
        sys.exit(f"job {JOBS[key]} is not deployed: python3 -I poc.py deploy")
    print(f"    running {JOBS[key]} ...")
    r = w.jobs.run_now_and_wait(jid)
    print(f"    {JOBS[key]}: {result(r)}")
    return r


def wait_for_compile(t0):
    """Wait for the compiler run that the export change started (the table trigger fires about a minute later)."""
    jid = job_id("compiler")
    if not jid:
        compile_now()
        return
    print("    waiting for the compiler job that the change starts (table trigger) ...")
    for _ in range(80):  # up to 20 minutes
        runs = [r for r in w.jobs.list_runs(job_id=jid, start_time_from=t0)]
        done = [r for r in runs if r.state.life_cycle_state.value in ("TERMINATED", "INTERNAL_ERROR", "SKIPPED")]
        if done and not [r for r in runs if r not in done]:
            outcome = result(done[0])  # not named "result": that would hide the result() helper in this function
            print(f"    compiler run finished: {outcome}")
            if outcome != "SUCCESS":
                sys.exit("The compiler run FAILED: the change has not reached the views. See Jobs & Pipelines > "
                         f"{JOBS['compiler']} > Runs, fix the cause, then python3 -I poc.py run-job compiler")
            return
        if not runs and time.time() * 1000 - t0 > 4 * 60 * 1000:  # no trigger after 4 minutes: run it here
            if result(run_job("compiler")) != "SUCCESS":
                sys.exit("The compiler run FAILED: the change has not reached the views.")
            return
        time.sleep(15)
    sys.exit(f"No finished compiler run after 20 minutes: check Jobs & Pipelines > {JOBS['compiler']}")


# ------------------------------------------------------------------------------------------------ commands
def deploy(a):
    ensure_warehouse()
    ensure_principals()
    ensure_rt_warehouse()
    if a.rebuild_data or not data_ok():
        ensure_data()
    else:
        step(f"catalog {C}: base and export tables in place, data kept (--rebuild-data reloads it)")
    ensure_grants()
    ensure_jobs()  # first, so the compile below runs the code just uploaded with the bundle
    compile_now()
    bad = []
    if a.verify:
        step("verify")  # the compile above ran the compiler job (it exits if the job failed)
        for t in ("test_access.py", "test_security.py"):
            if subprocess.run([sys.executable, "-I", os.path.join(HERE, "tests", t)], check=False).returncode != 0:
                bad.append(t)
    if bad:
        sys.exit(f"\nDeploy finished, but verification FAILED: {', '.join(bad)}")
    print("\nDeploy finished. Check with: python3 -I poc.py status")


def status(a):
    wh = warehouse()
    print(f"SQL warehouse   {config.WAREHOUSE_NAME}: {'present (' + wh.id + ')' if wh else 'absent'}")
    if config.RT_WAREHOUSE_NAME:
        rt = warehouse(config.RT_WAREHOUSE_NAME)
        print(f"Real-Time       {config.RT_WAREHOUSE_NAME}: {'present (' + rt.id + ')' if rt else 'absent'}")
    sps = {k: find_sp(config.principal_name(k)) for k in KEYS}
    print(f"principals      {sum(v is not None for v in sps.values())} of {len(KEYS)} present; secrets file "
          f"{'present' if os.path.exists(config.SP_CREDS) else 'absent'}")
    print(f"catalog         {C}: {'present' if catalog_exists() else 'absent'}")
    if wh and catalog_exists():
        n = q(f"SELECT count(*) FROM {C}.information_schema.views WHERE table_schema = '{config.AUDIT}'")[0][0]
        print(f"audit views     {C}.{config.AUDIT}: {n}; auditors: {', '.join(config.AUDITORS) or 'none but you'}")
    for k in JOBS:
        print(f"job             {JOBS[k]}: {'present' if job_id(k) else 'absent'}")
    if wh and catalog_exists():
        mine = q(f"SELECT array_join(collect_set(persona_id), ', ') FROM {X}.assoc_roles_dim WHERE user_principal = '{config.ADMIN}'")
        print(f"your login      {config.ADMIN}: personas = {mine[0][0] or 'none (you see nothing in the views)'}")


def destroy(a):
    if not a.yes:
        sys.exit("This removes everything the deploy created. Run again with --yes to confirm.")
    step("jobs")
    if job_id("compiler") or job_id("guardian"):
        if bundle("destroy", "--auto-approve").returncode != 0:  # never orphan jobs that run as a deleted principal
            sys.exit("bundle destroy failed: nothing else was removed. Fix the cause (section 6) and run destroy again.")
    import shutil
    shutil.rmtree(os.path.join(HERE, ".databricks"), ignore_errors=True)  # the CLI's local bundle state: no residue
    step(f"catalog {C}")
    if catalog_exists():
        q(f"DROP CATALOG {C} CASCADE")
        print("    dropped")
    big = config.LARGE["catalog"]  # the optional large data set (large_data.py) relies on the principals below
    if big != C and any(c.name == big for c in w.catalogs.list()):
        step(f"large data set: catalog {big}")
        q(f"DROP CATALOG {big} CASCADE")
        print("    dropped")
    step("service principals (deleted in the account: a workspace-only delete would be re-synced)")
    acct = AccountClient(profile=config.ACCOUNT_PROFILE)
    for k in KEYS:
        sp = find_sp(config.principal_name(k))
        if sp is None:
            continue
        for s in acct.service_principals.list(filter=f'applicationId eq "{sp.application_id}"'):
            acct.service_principals.delete(s.id)
        print(f"    deleted {config.principal_name(k)}")
    if os.path.exists(config.SP_CREDS):
        os.remove(config.SP_CREDS)
        print(f"    removed {config.SP_CREDS}")
    for name in (config.WAREHOUSE_NAME, config.RT_WAREHOUSE_NAME, config.LARGE["warehouse_name"]):
        wh = warehouse(name)
        if wh:
            step(f"SQL warehouse {name}")
            w.warehouses.delete(wh.id)
            print("    deleted")
    print("\nDestroy finished. Check with: python3 -I poc.py status")


def act_as(a):
    me = config.ADMIN
    if a.persona != "none" and a.persona not in config.PERSONA_KEYS:
        sys.exit(f"unknown persona; choose one of {', '.join(config.PERSONA_KEYS)} or none")
    if a.persona not in ("none", "no_persona") and not config.app_id(a.persona):
        sys.exit(f"{a.persona} is not in {config.SP_CREDS}: run python3 -I poc.py deploy first")
    t0 = int(time.time() * 1000)
    step(f"policy rows for {me}: " + ("none" if a.persona in ("none", "no_persona") else f"those of {a.persona}"))
    if not warehouse() or not catalog_exists():  # nothing deployed (for example after destroy): nothing to switch
        if a.persona in ("none", "no_persona"):
            print("    the POC is not deployed: nothing to do")
            return
        sys.exit("The POC is not deployed: run python3 -I poc.py deploy first.")
    had = int(q(f"SELECT count(*) FROM {X}.assoc_roles_dim WHERE user_principal = '{me}'")[0][0])
    if a.persona in ("none", "no_persona") and had == 0:  # nothing to change, so no compile would start
        print(f"    {me} already has no policy rows: nothing to do")
        return
    q(f"DELETE FROM {X}.assoc_roles_dim WHERE user_principal = '{me}'")
    if a.persona not in ("none", "no_persona"):
        q(f"""INSERT INTO {X}.assoc_roles_dim SELECT '{me}', win, persona_id, scope_value, current_timestamp()
              FROM {X}.assoc_roles_dim WHERE user_principal = '{config.app_id(a.persona)}'""")
    wait_for_compile(t0)
    print(f"\nDone: in the SQL editor, {me} now sees what {a.persona} sees. Back: python3 -I poc.py act-as none")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy")
    d.add_argument("--verify", action="store_true")
    d.add_argument("--rebuild-data", action="store_true")
    sub.add_parser("status")
    x = sub.add_parser("destroy")
    x.add_argument("--yes", action="store_true")
    s = sub.add_parser("act-as")
    s.add_argument("persona")
    r = sub.add_parser("run-job")
    r.add_argument("job", nargs="?", default="compiler", choices=list(JOBS))
    a = ap.parse_args()
    {"deploy": deploy, "status": status, "destroy": destroy, "act-as": act_as,
     "run-job": lambda a: run_job(a.job)}[a.cmd](a)
