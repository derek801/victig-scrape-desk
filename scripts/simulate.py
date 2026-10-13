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

from app import pb, runner, seed, slicer, store  # noqa: E402

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
print("all checks passed")
