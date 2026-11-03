"""Row tracking for the Clay tables the scrapes feed (one table per rep, filled by Clay's "Pull in Leads from
PhantomBuster" column). Clay has no public way to read a table's row count or to duplicate a table, so the desk
keeps a running estimate: the count someone typed in from Clay, plus every profile that rep's phantom has scraped
since. It alerts as a table nears Clay's 50,000-row cap; duplicating stays a step done in Clay."""
import datetime as dt

from . import alerts, store

LIMIT = 50_000
LEVELS = (95, 90, 80)             # alert thresholds, highest first
LINKS = {"blake": "https://app.clay.com/workspaces/484469/workbooks/wb_0tmv2nagmVBxouV3B3x/tables/"
                  "t_0tmv2nbbe9eXvxxu8Vf/views/gv_9JnJZA5Xvrg8"}
URL_PREFIX = "https://app.clay.com/"


def ensure():
    """Every enabled account has one active table row; fill in known links."""
    for a in store.q("SELECT key,name FROM accounts WHERE enabled=1"):
        t = store.q1("SELECT * FROM clay_tables WHERE account_key=? AND active=1", (a["key"],))
        if not t:
            store.x("INSERT INTO clay_tables(account_key,name,url,row_limit,active,created_at) VALUES(?,?,?,?,1,?)",
                    (a["key"], a["name"].split()[0] + "'s table", LINKS.get(a["key"]), LIMIT, store.iso()))
        elif not t["url"] and LINKS.get(a["key"]):
            store.x("UPDATE clay_tables SET url=? WHERE id=?", (LINKS[a["key"]], t["id"]))


def rows_used(t):
    if t["baseline_rows"] is None:
        return None
    got = store.q1("SELECT COALESCE(SUM(got),0) g FROM runs WHERE account_key=? AND launched_at>?",
                   (t["account_key"], t["baseline_at"]))["g"]
    return t["baseline_rows"] + got


def per_working_day(key):
    """Average profiles scraped per weekday over the last 14 days (10 weekdays)."""
    since = (store.now() - dt.timedelta(days=14)).isoformat(timespec="seconds")
    g = store.q1("SELECT COALESCE(SUM(got),0) g FROM runs WHERE account_key=? AND launched_at>=?", (key, since))["g"]
    return g / 10 if g else None


def overview():
    out = []
    for t in store.q("SELECT * FROM clay_tables WHERE active=1 ORDER BY account_key"):
        rows, lim = rows_used(t), t["row_limit"] or LIMIT
        pct = round(rows / lim * 100, 1) if rows is not None else None
        rate = per_working_day(t["account_key"])
        left = int((lim - rows) / rate) if rows is not None and rate else None
        t.update(rows=rows, limit=lim, pct=pct, per_day=round(rate) if rate else None, days_left=left,
                 level=next((l for l in LEVELS if pct is not None and pct >= l), 0))
        out.append(t)
    return out


def _clear_levels(tid, quiet=True, text=None):
    for l in LEVELS:
        alerts.clear(f"clay:{tid}:{l}", text, quiet=quiet)


def check():
    """Alert once per table per level (80 / 90 / 95%), re-announced only as a reminder."""
    ensure()
    state = store.kv_get("clay_levels", {})
    for t in overview():
        tid, lvl, prev = str(t["id"]), t["level"], state.get(str(t["id"]), 0)
        if lvl == prev and not lvl:
            continue
        if lvl != prev:
            if prev:
                alerts.clear(f"clay:{tid}:{prev}", f"{t['name']} is back under 80%" if not lvl else None, quiet=bool(lvl))
        if lvl:
            eta = f", about {t['days_left']} working days left at the current pace" if t["days_left"] is not None else ""
            alerts.raise_alert(
                f"clay:{tid}:{lvl}",
                f"{t['name']} (Clay) is at {t['pct']:g}% ({t['rows']:,} of {t['limit']:,} rows){eta}. "
                + ("Duplicate it now." if lvl >= 90 else "Plan to duplicate it soon."),
                grace_min=0, remind_h=24 if lvl >= 95 else 48)
        state[tid] = lvl
    store.kv_set("clay_levels", state)


def _valid_url(url):
    return isinstance(url, str) and url.startswith(URL_PREFIX) and len(url) < 400


def update(tid, patch):
    t = store.q1("SELECT * FROM clay_tables WHERE id=? AND active=1", (tid,))
    if not t:
        raise KeyError(tid)
    if patch.get("rows") is not None:
        rows = int(patch["rows"])
        if not 0 <= rows <= 2 * LIMIT:
            raise ValueError("row count out of range")
        store.x("UPDATE clay_tables SET baseline_rows=?, baseline_at=? WHERE id=?", (rows, store.iso(), tid))
    if "url" in patch:
        if patch["url"] and not _valid_url(patch["url"]):
            raise ValueError(f"link must start with {URL_PREFIX}")
        store.x("UPDATE clay_tables SET url=? WHERE id=?", (patch["url"] or None, tid))
    if patch.get("name"):
        store.x("UPDATE clay_tables SET name=? WHERE id=?", (str(patch["name"])[:60], tid))
    check()


def replace(tid, url, name=None):
    """The table was duplicated in Clay: archive the old one and track the new one from zero."""
    t = store.q1("SELECT * FROM clay_tables WHERE id=? AND active=1", (tid,))
    if not t:
        raise KeyError(tid)
    if not _valid_url(url):
        raise ValueError(f"link must start with {URL_PREFIX}")
    _clear_levels(tid, quiet=False, text=f"{t['name']} was duplicated")
    state = store.kv_get("clay_levels", {})
    state.pop(str(tid), None)
    store.kv_set("clay_levels", state)
    store.x("UPDATE clay_tables SET active=0, archived_at=? WHERE id=?", (store.iso(), tid))
    store.x("INSERT INTO clay_tables(account_key,name,url,baseline_rows,baseline_at,row_limit,active,created_at) "
            "VALUES(?,?,?,0,?,?,1,?)", (t["account_key"], name or t["name"], url, store.iso(), t["row_limit"], store.iso()))
    store.log(f"{t['name']} replaced by a duplicate; tracking restarted at 0 rows")
