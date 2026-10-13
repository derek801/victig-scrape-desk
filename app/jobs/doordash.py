"""Campaign-10 hygiene job (Mimecast prune, stale prune, Clay-orphan attach, bounce guard), moved here from
Derek's PC. Off until the 'doordash_sync' setting is switched on; turn the PC's `doordash-lead-sync`
scheduled task off at the same moment so the two never both run."""
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from .. import alerts, config, store

SCRIPT = Path(__file__).with_name("doordash_sync.py")


def maybe_run():
    s = store.settings()
    if not s["doordash_sync"] or not config.BISON_KEY_VICTIG:
        return
    last = store.kv_get("doordash_last")
    if last and store.now() - dt.datetime.fromisoformat(last["at"]) < dt.timedelta(minutes=s["doordash_sync_every_min"]):
        return
    run()


def run():
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        p = subprocess.run([sys.executable, str(SCRIPT), "--apply"], capture_output=True, text=True, timeout=3000, env=env)
        out, code = (p.stdout + p.stderr)[-6000:], p.returncode
    except subprocess.TimeoutExpired:
        out, code = "timed out after 50 minutes", -1
    store.kv_set("doordash_last", {"at": store.iso(), "code": code, "output": out})
    hist = store.kv_get("doordash_history", [])
    hist = (hist + [{"at": store.iso(), "code": code, "summary": _summary(out)}])[-60:]
    store.kv_set("doordash_history", hist)
    if code != 0:
        alerts.raise_alert("doordash", f"DoorDash sync job failing (exit {code}): {out.strip().splitlines()[-1][:200] if out.strip() else ''}",
                           grace_min=24 * 60)
    else:
        alerts.clear("doordash")
    if "GUARD TRIPPED" in out:
        alerts.raise_alert("doordash_guard", "Bounce guard paused campaign 10 (bounce rate above 4% today).",
                           grace_min=0, remind_h=24)


def _summary(out):
    keep = [l for l in out.splitlines() if l.startswith(("PRUNE", "ATTACH", "PRUNED", "ATTACHED", "GUARD", "campaign 10"))]
    return " | ".join(keep)[:400]
