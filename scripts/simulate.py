"""Exercise the scheduler end to end against a fake PhantomBuster (no network, throwaway database).

Covers: launch in window, run polling, done / overflow split / capped slices, a login failure putting an
account in attention, the next-morning retry, the breaker, hand-run import, and region restart.
    python scripts/simulate.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["PB_API_KEY"] = "fake"
os.environ["RUN_SCHEDULER"] = "0"
# Never let a test touch real services: config would otherwise pick these up from .env.local and the
# alert tests would post to the real Google Chat space.
for _k in ("GOOGLE_CHAT_WEBHOOK", "SLACK_ALERT_WEBHOOK", "BISON_KEY_VICTIG", "HYPERTIDE_API_KEY", "ALERTS_MUTED"):
    os.environ[_k] = ""
os.environ["PUBLIC_URL"] = "https://desk.example.test"

from app import alerts, clay, config, health, inbox, pb, runner, seed, slicer, store  # noqa: E402

import requests  # noqa: E402


def _no_network(*a, **k):
    raise RuntimeError("simulate.py must not call the network")


requests.get = requests.post = requests.put = requests.patch = requests.delete = requests.request = _no_network

STATE = {"next": 1000, "containers": {}, "script": {}}


def fake_launch(agent_id, argument):
    STATE["next"] += 1
    cid = str(STATE["next"])
    STATE["containers"][cid] = {"agent": agent_id, "url": argument["salesNavigatorSearchUrl"], "status": "running"}
    return cid


def fake_container(cid):
    c = STATE["containers"][cid]
    return {"id": cid, "status": c["status"], "exitCode": c.get("exit", 0), "endType": c.get("endType", "finished")}


def fake_output(cid):
    c = STATE["containers"][cid]
    if c.get("login"):
        return "[error]❌ Session cookie not valid anymore. Please log in to LinkedIn to get a new one.\n"
    if c.get("already"):
        return f"{c['url']}\n[info_]ℹ️ This search has already been processed.\n"
    total = c.get("total", 1500)
    got = min(total, 2500)
    return (f"{c['url']}\n[info_]ℹ️ Total results count: {total}.\n[done_]✅ Got 25 leads from page 1. ({got} this launch)\n"
            "[error]❌ Your organization has reached the limit of 100,000 leads.\n[done_]✅ CSV saved at x\n"
            "[done_]✅ Data successfully saved!\n")


pb.launch = fake_launch
pb.container = fake_container
pb.output = fake_output
pb.containers = lambda agent_id, limit=20: []
pb.agent_argument = lambda agent_id: {"identities": [{"identityId": "x"}], "numberOfProfiles": 2500}
runner.in_window = lambda t=None: True


def finish_all(**kw):
    for c in STATE["containers"].values():
        if c["status"] == "running":
            c.update(status="finished", **kw)


def force_due():
    store.x("UPDATE accounts SET next_launch_at=?", (store.iso(store.now() - dt.timedelta(minutes=1)),))


SN = slicer.SN_CAP


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


seed.seed()
store.save_settings({"dry_run": False})
# today's real hand-launched runs count against the budget (Brent ran 1,791 today); start from a clean day
store.x("DELETE FROM runs WHERE launched_at>=?", (runner.today_start_utc(),))

# 1. due accounts launch once each
force_due()
runner.tick()
running = store.q("SELECT * FROM runs WHERE source='desk' AND status='launched'")
check(len(running) == 5, f"5 enabled accounts launched ({len(running)})")
runner.tick()
check(len(store.q("SELECT * FROM runs WHERE source='desk'")) == 5, "no second launch while a run is going")

# 2. normal finish -> slice done, profiles counted
finish_all(total=1500)
runner.tick()
done = store.q("SELECT * FROM slices WHERE status='done'")
check(len(done) == 5, f"5 slices done ({len(done)})")
n, got = runner.account_today("max")
check(got >= 1500 and n == 1, f"max today: {n} launch, {got} profiles")

# 3. overflow -> split into two pending children right behind it (budget raised so a 2nd run fits)
store.x("UPDATE accounts SET daily_budget=6000")
force_due()
runner.tick()
finish_all(total=4000)
runner.tick()
split = store.q("SELECT * FROM slices WHERE status IN ('split','capped')")
kids = store.q("SELECT * FROM slices WHERE parent_id IS NOT NULL")
check(len(split) == 5, f"overflowing slices split or capped ({len(split)})")
for a in store.q("SELECT * FROM accounts WHERE enabled=1"):
    nxt = slicer.next_slice(a["region_id"])
    parent = [s for s in split if s["region_id"] == a["region_id"]][0]
    if parent["status"] == "split":
        check(nxt["parent_id"] == parent["id"], f"{a['key']}: next slice is a child of the split one")

for parent in [s for s in split if s["status"] == "split"]:
    ch = [k for k in kids if k["parent_id"] == parent["id"]]
    pc, pb_ = json.loads(parent["counties"]), json.loads(parent["bands"])
    cc = [json.loads(k["counties"]) for k in ch]
    cb = [json.loads(k["bands"]) for k in ch]
    check(len(ch) == 2, f"slice {parent['id']} split into two children")
    if len(pc) > 1:
        check(all(b == pb_ for b in cb) and sorted(sum(cc, [])) == sorted(pc) and all(cc),
              f"slice {parent['id']}: multi-county overflow splits counties, keeps bands")
    elif len(pb_) > 1:
        check(all(c == pc for c in cc) and sum(cb, []) == pb_ and all(cb),
              f"slice {parent['id']}: single-county overflow splits headcount bands")
    else:
        ct = [json.loads(k["titles"]) for k in ch]
        check(all(c == pc for c in cc) and all(b == pb_ for b in cb) and all(ct)
              and sorted(sum(ct, [])) == sorted(store.settings()["titles"]),
              f"slice {parent['id']}: one county+band overflow splits title groups")

# 4. daily budget: with 2,000/day and ~4,000 already scraped, nothing more launches
store.x("UPDATE accounts SET daily_budget=2000")
force_due()
runner.tick()
check(not store.q("SELECT 1 FROM runs WHERE status='launched'"), "budget reached: nothing launched")

# 5. login failure -> attention; slice back in line
store.x("DELETE FROM runs WHERE source='desk'")   # pretend it's a new day
force_due()
runner.tick()
launched = store.q("SELECT * FROM runs WHERE status='launched'")
bad = launched[0]
STATE["containers"][bad["container_id"]]["login"] = True
STATE["containers"][bad["container_id"]]["exit"] = 1
finish_all()
runner.tick()
a = store.q1("SELECT * FROM accounts WHERE key=?", (bad["account_key"],))
check(a["status"] == "attention" and "LinkedIn" in a["status_note"], f"{a['key']} needs attention: {a['status_note'][:60]}")
sl = store.q1("SELECT * FROM slices WHERE id=?", (bad["slice_id"],))
check(sl["status"] == "pending" and sl["attempts"] == 1, "failed slice back to pending")

# 6. three accounts failing -> breaker
for key in [k["key"] for k in store.q("SELECT key FROM accounts WHERE enabled=1 AND status='ok'")][:2]:
    runner.set_attention(store.q1("SELECT * FROM accounts WHERE key=?", (key,)), "test")
runner.tick()
check(bool(runner.breaker()), "breaker tripped with 3 accounts down")
store.x("DELETE FROM runs WHERE source='desk' AND status='launched'")
force_due()
before = len(store.q("SELECT * FROM runs"))
runner.tick()
check(len(store.q("SELECT * FROM runs")) == before, "no launches while breaker is tripped")
runner.reset_breaker()

# 7. next-morning retry heals an attention account
store.x("UPDATE accounts SET status_since=? WHERE status='attention'", (store.iso(store.now() - dt.timedelta(days=1)),))
runner.retry_attention()
check(not store.q("SELECT 1 FROM accounts WHERE status='attention'"), "attention accounts retried next morning")

# 8. region restart when the queue is empty and the gap has passed
rid = 2
store.x("UPDATE slices SET status='done', started_at=? WHERE region_id=? AND status='pending'",
        (store.iso(store.now() - dt.timedelta(days=40)), rid))
check(slicer.maybe_restart(rid), "empty region restarts after the re-scrape gap")
check(store.q1("SELECT cycle FROM regions WHERE id=?", (rid,))["cycle"] == 2 and slicer.next_slice(rid), "cycle 2 queued")

# 9. dry run launches nothing
store.save_settings({"dry_run": True})
store.x("DELETE FROM runs WHERE status='launched'")
force_due()
before = len(store.q("SELECT * FROM runs"))
runner.tick()
check(len(store.q("SELECT * FROM runs")) == before, "dry run: nothing launched")

# 10. learning + launch guard: a county measured big is re-estimated big; an over-cap slice is split before launch
slicer.build_cycle(rid, 2)
sl = slicer.next_slice(rid)
fips0 = json.loads(sl["counties"])
pops = {c["fips"]: c["population"] for c in slicer.counties_for(sl)}
slicer._learn_counties(slicer.counties_for(sl), json.loads(sl["bands"]), 40 * SN)
check(slicer.current_est(sl) > SN * 2, "measured county ratio raises the slice estimate")
ready = slicer.next_ready(rid)
if len(fips0) > 1 or len(json.loads(sl["bands"])) > 1:
    check(ready["id"] != sl["id"] and store.q1("SELECT status FROM slices WHERE id=?", (sl["id"],))["status"] == "split",
          "over-cap slice split before launch")
    check(ready["parent_id"] is not None and (ready["est"] <= SN or (len(json.loads(ready["counties"])) == 1
          and len(json.loads(ready["bands"])) == 1)), "what launches next is a descendant, under the cap")

# 11. PhantomBuster refuses an already-processed search: slice done, account stays healthy
runner.reset_breaker()
store.save_settings({"dry_run": False})
store.x("UPDATE accounts SET status='ok', fail_streak=0")
store.x("DELETE FROM runs WHERE source='desk'")
force_due()
runner.tick()
r = store.q1("SELECT * FROM runs WHERE status='launched' ORDER BY id LIMIT 1")
STATE["containers"][r["container_id"]]["already"] = True
finish_all()
runner.tick()
a = store.q1("SELECT * FROM accounts WHERE key=?", (r["account_key"],))
check(a["status"] == "ok" and a["fail_streak"] == 0, "already-processed run does not fail the account")
check(store.q1("SELECT status FROM slices WHERE id=?", (r["slice_id"],))["status"] == "done", "already-processed slice closed as done")

# 12. first sync on a fresh database loads PhantomBuster's old runs as history, not "hand-launched"
def fake_hand(cid):
    STATE["containers"][cid] = {"agent": "x", "url": "https://www.linkedin.com/sales/search/people?query=x", "status": "finished"}
    return {"id": cid, "status": "finished", "exitCode": 0, "createdAt": int(dt.datetime.now().timestamp() * 1000) - 86400000 * 3}


pb.containers = lambda agent_id, limit=20: [fake_hand("9001")] if agent_id == "6910468073602201" else []
store.kv_set("runs_backfilled", None)
store.kv_set("hand_sync_at", None)
runner.sync_hand_runs()
r = store.q1("SELECT * FROM runs WHERE container_id='9001'")
check(r and r["source"] == "history", "first sync stores old PhantomBuster runs as history")
check(not store.q("SELECT 1 FROM events WHERE text LIKE '%hand-launched%' AND ts>=?", (store.iso(store.now() - dt.timedelta(minutes=1)),)),
      "first sync logs no hand-launched runs")
pb.containers = lambda agent_id, limit=20: [fake_hand("9002")] if agent_id == "6910468073602201" else []
store.kv_set("hand_sync_at", None)
runner.sync_hand_runs()
check(store.q1("SELECT source FROM runs WHERE container_id='9002'")["source"] == "hand", "later runs still count as hand-launched")

# 13. title-group splitting ends in a cap; big counties get the estimate uplift until measured
solo = store.q1("SELECT * FROM slices WHERE status='pending' AND counties NOT LIKE '%,%' AND bands NOT LIKE '%,%' LIMIT 1")
if solo:
    t = ["Recruiter"]
    store.x("UPDATE slices SET titles=? WHERE id=?", (json.dumps(t), solo["id"]))
    solo = store.q1("SELECT * FROM slices WHERE id=?", (solo["id"],))
    check(slicer.record_result(solo, 9000, 2500) == "capped", "one county, band and title left: capped")
big, small = slicer.county_est("x", 1_000_000, ["D"], 1.0, {}), slicer.county_est("y", 400_000, ["D"], 1.0, {})
check(abs(big - small * 2.5 * 1.5) <= 3, "counties of 500k+ get the 1.5x estimate uplift until measured")

# 14. Clay table tracking: starting count + scraped since, alerts by level, replace restarts at zero
clay.ensure()
tabs = {t["account_key"]: t for t in clay.overview()}
check(len(tabs) == 5 and tabs["blake"]["url"] and tabs["blake"]["rows"] is None, "five Clay tables, Blake's linked, counts unset")
tid = tabs["max"]["id"]
clay.update(tid, {"rows": 40000})
store.x("INSERT INTO runs(account_key,container_id,launched_at,status,got,source) VALUES('max','clay-t1',?, 'finished', 2000, 'desk')",
        (store.iso(store.now() + dt.timedelta(minutes=1)),))
t = [x for x in clay.overview() if x["id"] == tid][0]
check(t["rows"] == 42000 and t["level"] == 80, "count = starting count + profiles scraped since (84% -> level 80)")
clay.check()
check(f"clay:{tid}:80" in alerts.open_alerts(), "80% alert raised")
clay.update(tid, {"rows": 44000})
clay.check()
open_ = alerts.open_alerts()
check(f"clay:{tid}:90" in open_ and f"clay:{tid}:80" not in open_, "crossing 90% replaces the 80% alert")
store.x("DELETE FROM runs WHERE container_id='clay-t1'")   # test run was stamped in the future
clay.replace(tid, "https://app.clay.com/workspaces/1/tables/t_new")
t2 = [x for x in clay.overview() if x["account_key"] == "max"][0]
check(t2["id"] != tid and t2["rows"] == 0 and not [k for k in alerts.open_alerts() if k.startswith(f"clay:{tid}:")],
      "duplicate: new table tracked from 0, old alerts cleared")
try:
    clay.update(t2["id"], {"url": "https://evil.example/x"})
    check(False, "non-Clay link rejected")
except ValueError:
    check(True, "non-Clay link rejected")
clay.update(t2["id"], {"rows": 49500})
check(clay.held("max") and not clay.held("blake"), "table at 99%: that account's launches are held")
runner.in_window = lambda t=None: True
store.x("UPDATE accounts SET status='ok', enabled=1, next_launch_at=?, daily_budget=2000 WHERE key='max'", (store.iso(store.now() - dt.timedelta(minutes=5)),))
store.x("DELETE FROM runs WHERE account_key='max' AND source='desk'")
check(not runner.due(store.q1("SELECT * FROM accounts WHERE key='max'")), "held account is not due")
clay.update(t2["id"], {"rows": 100})
check(not clay.held("max") and runner.due(store.q1("SELECT * FROM accounts WHERE key='max'")), "hold lifts once the count is back down")

# 15. Inbox Health: lifetime bounce per domain, bands, grouped alerts, Google Chat card
def mk(i, dom, sent, bounced, status="Connected", replied=0):
    return {"id": i, "email": f"u{i}@{dom}", "status": status, "warmup_enabled": True, "daily_limit": 5,
            "emails_sent_count": sent, "bounced_count": bounced, "total_replied_count": replied}


fake = ([mk(i, "good.com", 200, 2, replied=4) for i in range(10)] + [mk(100 + i, "warn.com", 200, 5, replied=2 if i < 6 else 1) for i in range(10)]
        + [mk(200 + i, "bad.com", 200, 8) for i in range(10)] + [mk(300 + i, "tiny.com", 10, 5) for i in range(3)]
        + [mk(400, "good.com", 200, 2, "Not connected")])
config.BISON_KEY_VICTIG = "x"
inbox.fetch_senders = lambda: fake
inbox.fetch_hypertide = lambda: {"bad.com": {"status": "NPC", "created": "2025-06-01", "tool": "Email Bison"}}
config.HYPERTIDE_API_KEY = "y"
inbox.fetch_warmup = lambda: {x["id"]: (92 if x["email"].endswith("good.com") else 70 if x["email"].endswith("warn.com") else 55) for x in fake}
inbox.fetch_dns = lambda d: {"spf": True, "dmarc": True, "mx": True, "dkim": True}
check(inbox.sync(), "inbox sync runs")
o = inbox.overview()
by = {d["domain"]: d for d in o["domains"]}
check(by["good.com"]["band"] == "ok" and by["warn.com"]["band"] == "warn" and by["bad.com"]["band"] == "critical"
      and by["tiny.com"]["band"] == "new", "lifetime bands: ok <2%, warning 2-3%, critical 3%+, too few sends ignored")
check(by["good.com"]["reply_band"] == "ok" and by["warn.com"]["reply_band"] == "warn" and by["bad.com"]["reply_band"] == "critical"
      and by["tiny.com"]["reply_band"] == "new", "reply bands: lower is worse (ok >=1.0%, warning 0.6-1.0%, critical <0.6%, too few sends ignored)")
check(by["good.com"]["reply_rate"] == 1.82 and by["warn.com"]["reply_rate"] == 0.8 and by["bad.com"]["reply_rate"] == 0.0,
      "reply rate = replies / sends for the whole domain")
check([d["domain"] for d in o["domains"]] == ["bad.com", "warn.com", "good.com", "tiny.com"],
      "domains sort by worst health score first, too-few-sends last")
hs = {d["domain"]: d["health"] for d in o["domains"]}
check(hs["good.com"]["zone"] == "healthy" and hs["bad.com"]["score"] < hs["warn.com"]["score"] < hs["good.com"]["score"]
      and hs["tiny.com"]["zone"] == "new" and hs["bad.com"]["drag"]["key"] == "reply" and hs["good.com"]["basis"] == "lifetime",
      "health score orders good > warn > bad; bad's biggest drag is reply rate; too few sends = new")
check(hs["good.com"]["coverage"] == 58 and o["zones"]["new"] == 1, "complaints and blacklists are unreadable, volume needs a window, sample has no interested replies: coverage 58%; zone counts")
check(o["reply_rate"] is not None and o["reply_counts"] == {"critical": 1, "warn": 1, "ok": 1, "new": 1}, "overall reply rate and band counts")
check(by["bad.com"]["ht_status"] == "NPC" and o["disconnected"] == 1, "Hypertide status joined; disconnected counted")
check(alerts.due_alerts() == {}, "domain trouble raises no alerts: only the automation breaking does")

# 16. daily snapshots give a trend: older day with a lower rate -> positive points, recent-sends rate
old_day = (dt.date.fromisoformat(inbox.today()) - dt.timedelta(days=7)).isoformat()
store.x("DELETE FROM inbox_history")
store.xmany("INSERT INTO inbox_history(day,domain,sent,bounced,mailboxes,connected,replied,interested,score) VALUES(?,?,?,?,?,?,?,?,?)",
            [(old_day, "bad.com", 1000, 20, 10, 10, 0, 0, 60.0), (old_day, "good.com", 2200, 22, 11, 10, 44, 0, 90.0)])
inbox.sync()
by = {d["domain"]: d for d in inbox.overview()["domains"]}
check(by["bad.com"]["health"]["basis"] == "7d" and by["bad.com"]["health"]["zone"] == "retire"
      and any("bounce rate" in r for r in by["bad.com"]["health"]["hard_fail"]), "7-day window: 6% bounce over 1,000 sends is a hard-fail to Retire")
check(by["good.com"]["health"]["basis"] == "lifetime" and by["bad.com"]["health"]["trend"] is not None
      and by["bad.com"]["health"]["trend"] < 0, "too few sends in the window falls back to lifetime; score trend vs a week ago")
check(by["bad.com"]["trend"] and by["bad.com"]["trend"]["pp"] > 1.5 and by["bad.com"]["trend"]["days"] == 7
      and by["bad.com"]["recent_rate"] is not None, "trend: a worsening domain shows positive points and its recent-sends rate")
check(by["good.com"]["trend"] and abs(by["good.com"]["trend"]["pp"]) < 0.5, "trend: a steady domain stays flat")
check(len(store.q("SELECT 1 FROM inbox_history WHERE day=?", (inbox.today(),))) == 4, "one snapshot row per domain per day")
inbox.sync()
check(len(store.q("SELECT 1 FROM inbox_history WHERE day=?", (inbox.today(),))) == 4, "a second sync the same day replaces, not duplicates")

# 16b. scoring rules on plain numbers
FL = {"reply_rate": 1.0, "positive_rate": 0.2}
base = {"sent": 1000, "bounced": 5, "replied": 12, "interested": 3, "basis": "7d", "mailboxes": 10, "warmup": 95,
        "dns": {"spf": True, "dmarc": True, "mx": True, "dkim": True}, "util": 0.5}
r0 = health.score_domain(base, FL)
check(r0["zone"] == "healthy" and r0["score"] >= 90, "a clean domain at fleet reply rate scores Healthy")
r1 = health.score_domain({**base, "replied": 2, "interested": 0}, FL)
check(r1["score"] < r0["score"] and r1["drag"]["key"] == "reply", "low reply rate is the biggest drag and lowers the score")
check(health.score_domain({**base, "complaint_pct": 0.35}, FL)["zone"] == "retire", "spam complaints at 0.3% hard-fail to Retire")
cap = health.score_domain({**base, "bounced": 25}, FL)
check(cap["zone"] == "danger" and cap["capped"], "2.5% bounce over a 7-day window caps the domain at Danger")
check(health.score_domain({**base, "bounced": 60}, FL)["zone"] == "retire", "6% bounce over a 7-day window hard-fails to Retire")
check(health.score_domain({**base, "dns": {"spf": False, "dmarc": False, "mx": True, "dkim": False}}, FL)["zone"] == "retire",
      "no SPF and no DMARC hard-fails")
check(health.score_domain({**base, "dns": {"error": "timeout"}}, FL)["zone"] == "healthy", "unreadable DNS is left out, not counted as a fail")
check(health.score_domain({**base, "basis": "lifetime", "bounced": 60}, FL)["zone"] != "retire", "lifetime numbers never hard-fail on bounce alone")
check(health.score_domain({**base, "blacklisted": True}, FL)["zone"] == "retire", "a major blacklist listing hard-fails")
check(health.score_domain({"sent": 0}, FL)["zone"] == "new", "no data = new")

# 17. the desk posts nowhere; /api/check hands problems to the Make daily check behind a token
import types  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from app import main as desk  # noqa: E402

check(not hasattr(alerts, "_post") and not hasattr(config, "GOOGLE_CHAT_WEBHOOK") and not hasattr(config, "SLACK_ALERT_WEBHOOK"),
      "no chat or slack posting code is left in the desk")
req = lambda t: types.SimpleNamespace(headers={"x-check-token": t} if t is not None else {})  # noqa: E731
config.DESK_CHECK_TOKEN = ""
try:
    desk.api_check(req("anything")); denied = False
except HTTPException as e:
    denied = e.status_code == 401
check(denied, "/api/check refuses everyone while no token is configured")
config.DESK_CHECK_TOKEN = "t0ken"
try:
    desk.api_check(req("wrong")); denied = False
except HTTPException as e:
    denied = e.status_code == 401
check(denied, "/api/check refuses a wrong token")
r = desk.api_check(req("t0ken"))
check(r["ok"] and r["problems"] == [], "/api/check does not report individual burnt domains")
store.kv_set("alerts_open", {})
store.kv_set("heartbeat", store.iso())
store.kv_set("inbox_sync", {"at": store.iso()})
alerts.raise_alert("acct:max", "Max's phantom is stuck", grace_min=60)
check(desk.api_check(req("t0ken"))["problems"] == [], "an alert still inside its grace period is not reported")
alerts.raise_alert("acct:max", "Max's phantom is stuck", grace_min=0)
check(desk.api_check(req("t0ken"))["problems"] == ["Max's phantom is stuck"], "an alert past its grace period is reported")
store.kv_set("alerts_open", {})
check(desk.api_check(req("t0ken"))["ok"], "no problems: ok is true and the list is empty")
store.kv_set("heartbeat", (store.now() - dt.timedelta(hours=2)).isoformat(timespec="seconds"))
config.RUN_SCHEDULER = True
check(any("scheduler" in t for t in desk.api_check(req("t0ken"))["problems"]), "a silent scheduler is itself a problem")
store.kv_set("heartbeat", store.iso())
store.kv_set("inbox_sync", {"at": (store.now() - dt.timedelta(hours=20)).isoformat(timespec="seconds")})
check(any("Inbox Health" in t for t in desk.api_check(req("t0ken"))["problems"]), "a stale Bison sync is a problem")
print("all checks passed")
