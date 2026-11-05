"""Run SQL on the POC's SQL warehouse, as the admin (your CLI login), the compiler or a test user.
  python3 -I dbsql.py [--as <key>] "SQL"         one statement
  python3 -I dbsql.py [--as <key>] -f file.sql   every statement in a file (split on ';' at the end of a line)
  add --rt to run on the Lakehouse Real-Time warehouse (read queries only); POC_WAREHOUSE=<name> does the same for
  any script that uses this module (for example the test suites)
<key> is a persona key from config.PERSONAS or "compiler". {{PLACEHOLDERS}} are filled in by config.render().
Inside a Databricks job the client authenticates as the job's run-as identity (the compiler)."""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(globals().get("__file__") or sys.argv[0])))
import config  # noqa: E402
from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service import sql as sqlsvc  # noqa: E402

_wh = {}


def client(key=None):
    if key is None and os.environ.get("DATABRICKS_RUNTIME_VERSION"):
        return WorkspaceClient()  # job task: its run-as identity
    if key is None:
        return WorkspaceClient(profile=config.PROFILE)
    c = config.creds().get(key)
    if not c:
        sys.exit(f"No secret for '{key}' in {config.SP_CREDS}: run python3 -I poc.py deploy first.")
    return WorkspaceClient(host=config.HOST, client_id=c["application_id"], client_secret=c["secret"])


def warehouse_id(w):
    """The POC warehouse, found by name (its ID changes whenever it is recreated)."""
    if "id" not in _wh:
        name = os.environ.get("POC_WAREHOUSE") or config.WAREHOUSE_NAME
        found = [x.id for x in w.warehouses.list() if x.name == name]
        if not found:
            raise RuntimeError(f"SQL warehouse '{name}' not found: run python3 -I poc.py deploy")
        _wh["id"] = found[0]
    return _wh["id"]


def run(w, sql, quiet=False):
    """Run one statement and wait. Returns (column names, rows). Raises RuntimeError with the server's message."""
    sql = config.render(sql)
    # JSON rows inline: a Real-Time warehouse returns Arrow by default
    r = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse_id(w), wait_timeout="50s",
                                                on_wait_timeout=sqlsvc.ExecuteStatementRequestOnWaitTimeout.CONTINUE,
                                                format=sqlsvc.Format.JSON_ARRAY, disposition=sqlsvc.Disposition.INLINE)
    while r.status.state in (sqlsvc.StatementState.PENDING, sqlsvc.StatementState.RUNNING):
        time.sleep(1)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state != sqlsvc.StatementState.SUCCEEDED:
        raise RuntimeError(r.status.error.message if r.status.error else str(r.status.state))
    cols = [c.name for c in r.manifest.schema.columns] if r.manifest and r.manifest.schema.columns else []
    rows = list(r.result.data_array or []) if r.result else []
    chunk = r.result.next_chunk_index if r.result else None
    while chunk is not None:
        part = w.statement_execution.get_statement_result_chunk_n(r.statement_id, chunk)
        rows += part.data_array or []
        chunk = part.next_chunk_index
    if not quiet:
        print(" | ".join(cols))
        for row in rows[:50]:
            print(" | ".join("NULL" if v is None else str(v) for v in row))
        if len(rows) > 50:
            print(f"... ({len(rows)} rows)")
    return cols, rows


def statements(text):
    """Split a SQL file into statements: a ';' that ends a line ends a statement. '--' comment lines are dropped."""
    out, cur = [], []
    for line in text.splitlines():
        if line.strip().startswith("--"):
            continue
        cur.append(line)
        if line.rstrip().endswith(";"):
            out.append("\n".join(cur).rstrip().rstrip(";"))
            cur = []
    if "".join(cur).strip():
        out.append("\n".join(cur))
    return [s for s in out if s.strip()]


def run_file(w, path, quiet=True):
    for s in statements(open(path).read()):
        if not quiet:
            print(">>", s.strip().splitlines()[0][:110])
        run(w, s, quiet=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="who", default=None)
    ap.add_argument("-f", dest="file")
    ap.add_argument("--rt", action="store_true", help="run on the Lakehouse Real-Time warehouse")
    ap.add_argument("sql", nargs="?")
    a = ap.parse_args()
    if a.rt:
        if not config.RT_WAREHOUSE_NAME:
            sys.exit("no realtime_warehouse_name in config.json")
        os.environ["POC_WAREHOUSE"] = config.RT_WAREHOUSE_NAME
    w = client(a.who)
    try:
        if a.file:
            run_file(w, a.file, quiet=False)
        else:
            run(w, a.sql)
    except RuntimeError as e:
        sys.exit(f"ERROR: {e}")
