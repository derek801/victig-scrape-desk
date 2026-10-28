"""The scheduler. One background thread ticks every minute:
  1. poll runs this app launched; close them out (slice done/split, account health)
  2. unless paused / dry run / breaker tripped: launch the next slice for any account that is due
"""
import datetime as dt
import json
import random
import threading
import time
import traceback
from zoneinfo import ZoneInfo

from . import alerts, config, geo, pb, searchurl, slicer, store

TICK_S = 60
MAX_SLICE_ATTEMPTS = 3
BREAKER_ACCOUNTS = 3        # this many accounts needing attention at once = stop everything


def local_now():
    return dt.datetime.now(ZoneInfo(config.TZ))


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def in_window(t=None):
    s = store.settings()
    t = t or local_now()
    if s["weekdays_only"] and t.weekday() >= 5:
        return False
    return _hm(s["window_start"]) <= (t.hour, t.minute) < _hm(s["window_end"])


def today_start_utc():
    t = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def account_today(key):
    """(desk launches today, profiles today from every run on this account incl. hand-launched ones).
    The profile budget counts hand runs too: LinkedIn sees one account, whoever pressed the button."""
    r = store.q1("SELECT SUM(CASE WHEN source='desk' THEN 1 ELSE 0 END) n, COALESCE(SUM(got),0) got FROM runs "
                 "WHERE account_key=? AND launched_at>=?", (key, today_start_utc()))
    return r["n"] or 0, r["got"]


def sync_hand_runs():
    """Every 15 minutes, record runs someone launched by hand in PhantomBuster (chart, budget, coverage)."""
    last = store.kv_get("hand_sync_at")
    if last and store.now() - dt.datetime.fromisoformat(last) < dt.timedelta(minutes=15):
        return
    store.kv_set("hand_sync_at", store.iso())
    known = {r["container_id"] for r in store.q("SELECT container_id FROM runs")}
    by_geo = {r["geo_id"]: r["fips"] for r in store.q("SELECT geo_id, fips FROM counties WHERE geo_id NOT IN ('', 'NONE')")}
    for a in store.q("SELECT * FROM accounts WHERE agent_id IS NOT NULL"):
        try:
            cs = pb.containers(a["agent_id"], limit=10)
        except pb.PBError:
            return
        for c in cs:
            cid = str(c.get("id"))
            if cid in known or c.get("status") != "finished":
                continue
            p = pb.parse_output(pb.output(cid))
            launched = dt.datetime.fromtimestamp(c["createdAt"] / 1000, dt.timezone.utc)
            store.x("INSERT OR IGNORE INTO runs(account_key,container_id,url,launched_at,finished_at,status,exit_code,"
                    "total,got,errors,source) VALUES(?,?,?,?,?,?,?,?,?,?,'hand')",
                    (a["key"], cid, p["url"], store.iso(launched), store.iso(), "finished" if c.get("exitCode") == 0 else "failed",
                     c.get("exitCode"), p["total"], p["got"], json.dumps(p["errors"])))
            if p["url"] and p["got"]:
                fips = [by_geo[g] for g, _ in searchurl.parse(p["url"])["regions"] if g in by_geo]
                if fips:
                    store.x("UPDATE counties SET last_scraped=MAX(COALESCE(last_scraped,''), ?) WHERE fips IN (%s)"
                            % ",".join("?" * len(fips)), [store.iso(launched)] + fips)
            store.log(f"{a['name']}: hand-launched run recorded ({p['got']:,} profiles)")


# ---- breaker -----------------------------------------------------------------------------------

def breaker():
    return store.kv_get("breaker")


def trip(reason):
    if not breaker():
        store.kv_set("breaker", {"reason": reason, "since": store.iso()})
        store.log(f"BREAKER TRIPPED: {reason}", "alert")
    alerts.raise_alert("breaker", f"all launches stopped: {reason}. Fix it, then press Reset on the page.",
                       grace_min=0, remind_h=12)


def reset_breaker():
    store.kv_set("breaker", None)
    store.kv_set("pb_api_fail", None)
    alerts.clear("breaker", "launches resumed")
    store.log("breaker reset")


# ---- account health ----------------------------------------------------------------------------

def set_attention(acct, note):
    store.x("UPDATE accounts SET status='attention', status_note=?, status_since=? WHERE key=?",
            (note, store.iso(), acct["key"]))
    store.log(f"{acct['name']}: needs attention: {note}", "warn")


def set_ok(acct):
    if acct["status"] == "attention":
        store.log(f"{acct['name']}: healthy again")
    store.x("UPDATE accounts SET status='ok', status_note=NULL, status_since=NULL, fail_streak=0 WHERE key=?",
            (acct["key"],))
    alerts.clear(f"acct:{acct['key']}")


def retry_attention():
    """An account in attention gets one fresh try each morning: if the rep fixed the login, it heals."""
    for a in store.q("SELECT * FROM accounts WHERE enabled=1 AND status='attention'"):
        if a["status_since"] and a["status_since"] < today_start_utc() and in_window():
            store.x("UPDATE accounts SET status='ok', fail_streak=1 WHERE key=?", (a["key"],))
            store.log(f"{a['name']}: retrying after yesterday's problem")


def health_checks():
    accts = store.q("SELECT * FROM accounts WHERE enabled=1")
    bad = [a for a in accts if a["status"] == "attention"]
    for a in bad:
        alerts.raise_alert(f"acct:{a['key']}", f"{a['name']}'s phantom is stuck: {a['status_note']}", grace_min=60)
    for a in accts:
        if a["status"] != "attention":
            alerts.clear(f"acct:{a['key']}")
    if len(bad) >= BREAKER_ACCOUNTS:
        trip(f"{len(bad)} accounts failing at once ({', '.join(a['name'] for a in bad)})")


# ---- polling -----------------------------------------------------------------------------------

def poll():
    for run in store.q("SELECT * FROM runs WHERE status IN ('launched','running') AND source='desk'"):
        try:
            c = pb.container(run["container_id"])
        except pb.PBError as e:
            store.log(f"poll {run['container_id']}: {e}", "warn")
            continue
        status = c.get("status")
        if status == "running" or status == "starting" or status == "queued":
            if run["status"] != "running":
                store.x("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
            continue
        finish(run, c)


def finish(run, c):
    acct = store.q1("SELECT * FROM accounts WHERE key=?", (run["account_key"],))
    sl = store.q1("SELECT * FROM slices WHERE id=?", (run["slice_id"],))
    p = pb.parse_output(pb.output(run["container_id"]))
    exit_code = c.get("exitCode")
    ok = exit_code == 0 and p["saved"] and not p["login_problem"]
    store.x("UPDATE runs SET status=?, finished_at=?, exit_code=?, total=?, got=?, errors=? WHERE id=?",
            ("finished" if ok else "failed", store.iso(), exit_code, p["total"], p["got"], json.dumps(p["errors"]),
             run["id"]))
    if p["storage_full"]:
        store.kv_set("pb_storage_full", store.iso())
    if ok:
        res = slicer.record_result(sl, p["total"], p["got"])
        store.log(f"{acct['name']}: slice {sl['id']} {res}, {p['got']:,} profiles of {p['total'] or 0:,}")
        set_ok(acct)
    else:
        attempts = (sl["attempts"] or 0) + 1
        store.x("UPDATE slices SET status=?, attempts=?, note=? WHERE id=?",
                ("failed" if attempts >= MAX_SLICE_ATTEMPTS else "pending", attempts,
                 ("; ".join(p["errors"]) or str(c.get("endType") or ""))[:300], sl["id"]))
        streak = (acct["fail_streak"] or 0) + 1
        store.x("UPDATE accounts SET fail_streak=? WHERE key=?", (streak, acct["key"]))
        if p["login_problem"]:
            set_attention(acct, "LinkedIn login expired or disconnected. "
                                f"{acct['name']} should open LinkedIn in Chrome and re-sync the PhantomBuster extension.")
        elif streak >= 2:
            set_attention(acct, f"{streak} failed runs in a row ({(p['errors'] or [c.get('endType')])[0]})")
        else:
            store.log(f"{acct['name']}: run failed ({c.get('endType')}), slice {sl['id']} goes back in line", "warn")
    _schedule_next(acct["key"], (10, 30))


def _schedule_next(key, gap_min):
    t = store.now() + dt.timedelta(minutes=random.randint(*gap_min))
    store.x("UPDATE accounts SET next_launch_at=? WHERE key=?", (store.iso(t), key))


# ---- launching ---------------------------------------------------------------------------------

def plan_for(acct):
    """What this account would launch next: (slice, url, counties) or None."""
    if not acct["region_id"]:
        return None
    sl = slicer.next_ready(acct["region_id"])
    if not sl:
        return None
    cs = slicer.counties_for(sl)
    url = searchurl.build(store.settings()["titles"], [(c["geo_id"], c["linkedin_text"]) for c in cs],
                          json.loads(sl["bands"]))
    return sl, url, cs


def due(acct):
    s = store.settings()
    if acct["status"] != "ok" or not acct["enabled"] or not in_window():
        return False
    n, got = account_today(acct["key"])
    if n >= s["max_launches_per_day"] or got >= acct["daily_budget"]:
        return False
    # A second run only fits if its expected size keeps today near the budget (a run can't be cut short
    # without leaving its slice half done), so one big slice a day, or several small ones.
    if got:
        sl = slicer.next_slice(acct["region_id"]) if acct["region_id"] else None
        if sl and got + min(sl["est"], slicer.SN_CAP) > acct["daily_budget"] * 1.25:
            return False
    if store.q1("SELECT 1 FROM runs WHERE account_key=? AND status IN ('launched','running')", (acct["key"],)):
        return False
    nla = acct["next_launch_at"]
    if not nla or nla < today_start_utc():
        # first look today: spread the morning launches over 90 minutes so accounts don't fire together
        _schedule_next(acct["key"], (0, 90))
        return False
    return store.now() >= dt.datetime.fromisoformat(nla)


def launch_one(acct):
    plan = plan_for(acct)
    if not plan and slicer.maybe_restart(acct["region_id"]):
        plan = plan_for(acct)
    if not plan:
        return
    sl, url, cs = plan
    try:
        latest = pb.containers(acct["agent_id"], limit=1)
        if latest and latest[0].get("status") == "running":   # a rep launched it by hand; wait
            _schedule_next(acct["key"], (20, 40))
            return
        arg = pb.agent_argument(acct["agent_id"])
        arg.update({"inputType": "salesNavigatorSearchUrl", "salesNavigatorSearchUrl": url,
                    "numberOfProfiles": 2500, "numberOfResultsPerSearch": 2500})
        cid = pb.launch(acct["agent_id"], arg)
    except pb.PBError as e:
        _api_failure(e)
        _schedule_next(acct["key"], (15, 30))
        return
    store.kv_set("pb_api_fail", None)
    alerts.clear("pb_api")
    store.x("UPDATE slices SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?", (store.iso(), sl["id"]))
    store.x("INSERT INTO runs(account_key,slice_id,container_id,url,launched_at,status) VALUES(?,?,?,?,?,'launched')",
            (acct["key"], sl["id"], cid, url, store.iso()))
    names = ", ".join(c["linkedin_text"].split(",")[0] for c in cs[:3]) + (f" +{len(cs) - 3}" if len(cs) > 3 else "")
    store.log(f"{acct['name']}: launched slice {sl['id']} ({names}; bands {','.join(json.loads(sl['bands']))}; "
              f"est {sl['est']:,})")


def _api_failure(e):
    f = store.kv_get("pb_api_fail") or {"since": store.iso(), "count": 0}
    f["count"] += 1
    f["last"] = str(e)[:300]
    store.kv_set("pb_api_fail", f)
    store.log(f"PhantomBuster API error: {e}", "warn")
    alerts.raise_alert("pb_api", f"PhantomBuster API keeps failing: {f['last']}", grace_min=60)
    if f["count"] >= 10:
        trip("PhantomBuster API failed 10 times in a row")


def tick():
    s = store.settings()
    if config.PB_API_KEY:
        poll()
        sync_hand_runs()
    retry_attention()
    health_checks()
    if s["paused"] or s["dry_run"] or breaker() or not config.PB_API_KEY:
        return
    for acct in store.q("SELECT * FROM accounts WHERE enabled=1 ORDER BY key"):
        if due(acct):
            launch_one(acct)


def loop():
    from .jobs import doordash
    while True:
        try:
            tick()
            store.kv_set("heartbeat", store.iso())
        except Exception:
            store.log("tick crashed: " + traceback.format_exc()[-800:], "error")
        try:
            geo.fill_some()
        except Exception:
            store.log("geo fill crashed: " + traceback.format_exc()[-400:], "error")
        try:
            doordash.maybe_run()
        except Exception:
            store.log("doordash job crashed: " + traceback.format_exc()[-800:], "error")
        time.sleep(TICK_S)


def start():
    threading.Thread(target=loop, daemon=True, name="scheduler").start()
