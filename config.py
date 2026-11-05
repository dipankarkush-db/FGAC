"""POC settings. Every workspace-specific value is in config.json next to this file (copy config.example.json and fill
it in; config.json is git-ignored); nothing else is hard-coded.
SQL files use {{PLACEHOLDERS}} that render() fills in, so the same files run in any workspace."""
import json
import os
import re
import sys

_DIR = os.path.dirname(os.path.abspath(globals().get("__file__") or sys.argv[0]))
for _d in (_DIR, os.path.join(_DIR, ".."), os.getcwd()):  # serverless job tasks may not set __file__
    if os.path.exists(os.path.join(_d, "config.json")):
        _DIR = os.path.abspath(_d)
        break
if not os.path.exists(os.path.join(_DIR, "config.json")):
    sys.exit("config.json not found: cp config.example.json config.json, then fill it in (docs/HOW_TO_RUN.md, section 2)")
CFG = json.load(open(os.path.join(_DIR, "config.json")))

# the POC tooling only (poc.py, the tests): the compiler job does not need these
PROFILE = CFG.get("profile")
HOST = (CFG.get("host") or "").rstrip("/")
ACCOUNT_ID = CFG.get("account_id")
ACCOUNT_PROFILE = CFG.get("account_profile")
# the admin principal or group: the guard leaves its privileges alone
ADMIN = CFG["admin"]
WAREHOUSE_NAME = CFG["warehouse_name"]
# Lakehouse Real-Time (Beta: the workspace's "Lakehouse RT" preview must be on): a read-only warehouse for the users'
# queries on the secured views. Optional; the compiler always runs on the serverless SQL warehouse (it writes).
RT_WAREHOUSE_NAME = CFG.get("realtime_warehouse_name")
CATALOG = os.environ.get("POC_CATALOG") or CFG["catalog"]  # POC_CATALOG: another catalog, e.g. the large data set's
SP_PREFIX = CFG.get("sp_prefix", "")
JOB_PREFIX = CFG.get("job_prefix", "fgac-")  # the jobs are <job_prefix>compiler and <job_prefix>guardian
# the optional large synthetic data set (large_data.py): its own catalog, and a bigger warehouse to build and compile it
LARGE = {"catalog": "fgac_large", "associates": 10_000_000, "warehouse_name": "fgac-large", "warehouse_size": "Large",
         **CFG.get("large_data", {})}
SP_CREDS = os.path.expanduser(CFG.get("sp_creds_file", "~/.fgac_principals.json"))
# what an unclassified column counts as: "highly_sensitive" (the default: anyone with Highly Sensitive access
# sees it, within that persona's scope) or "restricted" (the stricter reading: Highly Sensitive Restricted, never shown)
UNCLASSIFIED_RULE = CFG.get("unclassified_rule", "highly_sensitive")
# identities that load data (ETL, the policy export job): the guard leaves their privileges on the base and export
# schemas alone (never on the entitlements or the views). None in the POC (the admin loads the synthetic data).
ETL_PRINCIPALS = CFG.get("etl_principals", [])
# who may read the audit views (schema audit): users, groups, or service principal application IDs. The admin always
# can. They get USE SCHEMA and SELECT on the audit schema only: no HR data, no entitlement tables.
AUDITORS = CFG.get("auditors", [])
# the SQL text and error messages in the audit views (statements, restricted_reads): "redacted" (the default: string and
# number literals become ?, so values someone typed, such as an INSERT's or a WHERE ssn = '...', never reach the
# auditors) or "full"
AUDIT_STATEMENT_TEXT = CFG.get("audit_statement_text", "redacted")
assert AUDIT_STATEMENT_TEXT in ("redacted", "full"), f"audit_statement_text must be redacted or full, not {AUDIT_STATEMENT_TEXT}"
# assoc_dim.status values of associates who have left: their records stay governed by their last hierarchy, but their
# own login loses every entitlement, even if assoc_roles_dim still lists personas for it (fails closed)
TERMINATED_STATUSES = CFG.get("terminated_statuses", ["Terminated"])
TERMINATED_SQL = ", ".join("'" + v.replace("'", "''") + "'" for v in TERMINATED_STATUSES) or "NULL"  # for IN (...)
# Delta operations that change no data: an export whose newest versions are only these has not changed
MAINTENANCE_OPS = ("OPTIMIZE", "VACUUM START", "VACUUM END", "ANALYZE", "ANALYZE STATS", "COMPUTE STATISTICS",
                   "SET TBLPROPERTIES", "UNSET TBLPROPERTIES", "CLUSTER BY", "SET TAGS", "UNSET TAGS")
# how a view checks that a granting persona's scope covers a row: "join" (the design: the person's scope nodes joined once
# per query to a one-row-per-(associate, node) table) or "array" (the first form: arrays_overlap of the person's nodes
# with the associate's, on every row). Both give the same answers.
# POC_SCOPE_CHECK overrides it.
SCOPE_CHECK = os.environ.get("POC_SCOPE_CHECK") or CFG.get("scope_check", "join")
assert SCOPE_CHECK in ("array", "join"), f"scope_check must be array or join, not {SCOPE_CHECK}"

# schemas in the catalog
BASE = "hr_base"          # the real HR tables; only the compiler (and the admin) can read them
SECURE = "hr_secure"      # one generated secured view per base table, <table>_vw; the only thing users can read
EXPORTS = "policy"   # the tables the policy system exports (source of truth)
GOV = "governance"        # compiled entitlements and the compiler's log
AUDIT = "audit"           # generated audit views: who read which view, governance changes, entitlement history
AUDIT_VIEWS = ["view_reads", "restricted_reads", "statements", "governance_changes", "entitlement_changes", "compile_runs"]

VIEW_SUFFIX = "_vw"       # hr_secure.<table>_vw
EXPORT_TABLES = ["assoc_dim", "assoc_roles_dim", "persona", "persona_column_policy", "column_classification"]
COMPILER = "compiler"     # key of the compiler service principal in the secrets file
# test users: key -> (what they play, their win or None). Their policy rows are in setup/02_policy_exports.sql
PERSONAS = {
    "mm_market_abc": ("Market Manager (Markets A, B, C), associate 50001 with Self", 50001),
    "rhrd_south": ("Regional HR Director (Region South)", 20000002),
    "hrbp_hr": ("HR Business Partner (Department HR)", 20000003),
    "people_analyst": ("People Analyst (enterprise; Sensitive compensation only)", 20000004),
    "people_mgr": ("People Manager (reporting line), associate 50005 with Self", 50005),
    "senior_mgr": ("People Manager two levels up (50005 reports to him), associate 50001 with Self", 50001),
    "multi_persona": ("People Analyst + Market Manager (Market A): two personas", 20000006),
    "self_only": ("Associate 50004 with only the Self persona", 50004),
    "no_persona": ("No persona at all", None),
}
PERSONA_KEYS = list(PERSONAS)


def principal_name(key):
    return f"{SP_PREFIX}{key}"


def creds():
    """Secrets file: key -> {"application_id", "secret", "id"} for the personas and the compiler (local only)."""
    return json.load(open(SP_CREDS)) if os.path.exists(SP_CREDS) else {}


def app_id(key):
    c = creds().get(key)
    return c["application_id"] if c else None


def render(sql):
    """Fill {{CATALOG}}, {{BASE}}, {{SECURE}}, {{EXPORTS}}, {{GOV}}, {{AUDIT}}, {{ADMIN}} and {{SP:<key>}} (that principal's
    application ID) in a SQL text."""
    values = {"CATALOG": CATALOG, "BASE": BASE, "SECURE": SECURE, "EXPORTS": EXPORTS, "GOV": GOV, "AUDIT": AUDIT,
              "ADMIN": ADMIN}
    c = creds() if "{{SP:" in sql else {}

    def sub(m):
        k = m.group(1)
        return c[k[3:]]["application_id"] if k.startswith("SP:") else values[k]
    return re.sub(r"\{\{([A-Z_]+|SP:[a-z_]+)\}\}", sub, sql)
