"""Correctness: for every test user, every secured view and every column, what the user gets from the view must equal
what an independent oracle expects. The oracle reads only the policy exports and the base tables (never the
compiler's output) and applies the access rules directly:
  - a user with at least one persona other than Self sees every row; a user with only Self sees their own row; a user
    with no persona sees nothing
  - Non-Sensitive values are visible on every row the user sees
  - any other value is visible only if one persona the user holds both grants the column's (classification, category)
    pair and covers the row (Enterprise: every row; Market / Region / Department: the associate's place in assoc_dim;
    Reporting: everyone below that manager; Self: the user's own record)
  - an unclassified column counts as Highly Sensitive (any Highly Sensitive pair grants it), or is never visible when
    config "unclassified_rule" is "restricted"
  - a table with no associate key: only Enterprise personas reach its rows' sensitive values
Run: python3 -I tests/test_access.py [persona ...]"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import config  # noqa: E402
import dbsql  # noqa: E402

C, X, BASE, SECURE = config.CATALOG, f"{config.CATALOG}.{config.EXPORTS}", f"{config.CATALOG}.{config.BASE}", f"{config.CATALOG}.{config.SECURE}"
admin = dbsql.client()
STORY = (50001, 50002, 50003, 50004, 50005, 50006)


def a(sql):
    return dbsql.run(admin, sql, quiet=True)[1]


def norm(s):
    import re
    return re.sub(r"[^a-z0-9]+", "_", s.lower().replace("&", "and")) if s else None


# CLS (column classification): table -> column -> (term, grant key or None for unclassified)
CLS = {}
for qcn, term, cls, cat in a(f"SELECT lower(qualified_column_name), business_term, classification, data_security_category "
                             f"FROM {X}.column_classification"):
    parts = qcn.split(".")
    if len(parts) != 4 or parts[0] != C or parts[1] != config.BASE:  # skipped by the compiler too
        continue
    _, _, t, c = parts
    key = f"{norm(cls)}__{norm(cat)}" if cls and cat else (None if not cls and not cat else "denied_incomplete")
    if c in CLS.get(t, {}) and CLS[t][c][1] != key:  # conflicting classification rows: the compiler denies the column
        key = "denied_conflict"
    CLS.setdefault(t, {})[c] = (term, key)
TABLES = {t: [r[0] for r in a(f"SELECT column_name FROM {C}.information_schema.columns WHERE table_schema = '{config.BASE}' "
                              f"AND table_name = '{t}' ORDER BY ordinal_position")] for t in CLS
          if not os.environ.get("POC_TEST_TABLES") or t in os.environ["POC_TEST_TABLES"].split(",")}  # optional subset
TABLES = {t: cols for t, cols in TABLES.items() if cols}  # classification rows for a table that doesn't exist yet: no view, nothing to test
PERSONA_GRANTS = {}
for pid, cls, cat in a(f"SELECT persona_id, classification, data_security_category FROM {X}.persona_column_policy"):
    if cls and cat:  # a partial pair grants nothing
        PERSONA_GRANTS.setdefault(pid, set()).add(f"{norm(cls)}__{norm(cat)}")
# a persona listed with two scope types grants nothing
SCOPE_TYPE = dict(a(f"SELECT persona_id, min(scope_type) FROM {X}.persona GROUP BY persona_id HAVING count(DISTINCT scope_type) = 1"))


def roles(user):
    """persona -> (scope type, [scope values]), and the user's win."""
    out, wins = {}, set()
    for pid, w_, v in a(f"SELECT persona_id, win, scope_value FROM {X}.assoc_roles_dim WHERE lower(user_principal) = lower('{user}')"):
        if pid not in SCOPE_TYPE:  # a persona the exports don't define gives nothing (fails closed)
            continue
        out.setdefault(pid, (SCOPE_TYPE.get(pid), []))[1].append(v)
        if w_ is not None:  # as the compiler: count(DISTINCT win) ignores NULLs
            wins.add(w_)
    if wins and a(f"SELECT count(*) FROM {X}.assoc_dim WHERE win IN ({', '.join(str(w) for w in wins)}) "
                  f"AND status IN ({config.TERMINATED_SQL})")[0][0] != "0":
        return {}, None  # the person has left (any of their wins): no access at all
    return out, (next(iter(wins)) if len(wins) == 1 else None)  # two wins: no own record


def grants(pid):
    g = set(PERSONA_GRANTS.get(pid, ()))
    if config.UNCLASSIFIED_RULE == "highly_sensitive" and any(k.startswith("highly_sensitive__") for k in g):
        g.add("unclassified")
    return g


def scope_pred(scope_type, values, win, owner):
    """SQL predicate on base row b: does this persona's scope cover the row?"""
    vals = ", ".join("'" + v.replace("'", "''") + "'" for v in values if v is not None)
    if scope_type == "Enterprise":
        return "true"
    if owner is None:
        return "false"
    if scope_type == "Self":
        return f"b.`{owner}` = {win}" if win else "false"
    col = {"Market": "market", "Region": "region", "Department": "department"}.get(scope_type)
    if col and vals:
        return f"b.`{owner}` IN (SELECT win FROM {X}.assoc_dim WHERE {col} IN ({vals}))"
    if scope_type == "Reporting" and vals:
        return f"b.`{owner}` IN (SELECT win FROM {X}.assoc_dim WHERE arrays_overlap(reporting_chain, array({vals.replace(chr(39), '')})))"
    return "false"


def oracle(user, table):
    r, win = roles(user)
    cols = TABLES[table]
    owner = next((c for c in cols if CLS[table].get(c.lower(), (None,))[0] == "Associate ID"), None)
    if not r:
        row_pred = "false"
    elif any(st != "Self" for st, _ in r.values()):
        row_pred = "true"
    else:
        row_pred = f"b.`{owner}` = {win}" if owner and win else "false"
    exprs = [f"count(CASE WHEN {row_pred} THEN 1 END)"]
    cell = {}
    for c in cols:
        term, key = CLS[table].get(c.lower(), (None, None))
        if key and key.startswith("non_sensitive__"):
            pred = row_pred
        else:
            k = key or "unclassified"
            preds = [scope_pred(st, vals, win, owner) for pid, (st, vals) in r.items() if k in grants(pid)]
            pred = f"({row_pred}) AND ({' OR '.join(preds) or 'false'})"
        cell[c] = pred
        exprs.append(f"count(CASE WHEN {pred} THEN b.`{c}` END)")
    counts = [int(x) for x in a(f"SELECT {', '.join(exprs)} FROM {BASE}.`{table}` b")[0]]
    story = {}
    if owner:
        sel = ", ".join(f"CASE WHEN {cell[c]} THEN b.`{c}` END IS NOT NULL" for c in cols)
        for row in a(f"SELECT b.`{owner}`, {sel} FROM {BASE}.`{table}` b WHERE b.`{owner}` IN {STORY} AND ({row_pred})"):
            story[int(row[0])] = tuple(v == "true" for v in row[1:])
    return counts, story, owner


def actual(key, table, owner):
    u = dbsql.client(key)
    cols = TABLES[table]
    counts = [int(x) for x in dbsql.run(u, f"SELECT count(*), {', '.join(f'count(`{c}`)' for c in cols)} "
                                           f"FROM {SECURE}.`{table}{config.VIEW_SUFFIX}`", quiet=True)[1][0]]
    story = {}
    if owner:
        sel = ", ".join(f"`{c}` IS NOT NULL" for c in cols)
        for row in dbsql.run(u, f"SELECT `{owner}`, {sel} FROM {SECURE}.`{table}{config.VIEW_SUFFIX}` WHERE `{owner}` IN {STORY}", quiet=True)[1]:
            story[int(row[0])] = tuple(v == "true" for v in row[1:])
    return counts, story


if __name__ == "__main__":
    keys = sys.argv[1:] or config.PERSONA_KEYS
    failures = 0
    for key in keys:
        user = config.app_id(key)
        for table in sorted(TABLES):
            exp_counts, exp_story, owner = oracle(user, table)
            got_counts, got_story = actual(key, table, owner)
            ok = exp_counts == got_counts and exp_story == got_story
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {key:15} {table:24} rows {got_counts[0]:>7} (expected {exp_counts[0]:>7}); "
                  f"visible values per column {got_counts[1:]}")
            if not ok:
                print(f"      expected {exp_counts[1:]} story {exp_story}\n      got      {got_counts[1:]} story {got_story}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
