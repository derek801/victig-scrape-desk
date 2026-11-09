"""Alerts, quiet by design: a problem is a condition re-checked every pass; it counts as announced once it has
lasted `grace_min`, and clearing it logs one 'resolved' line. Nothing here talks to Chat, Slack or any other
service. Alerts show on the page and in the event log, and the RevOps Daily Connection Check (Make) reads them
from GET /api/check."""
import datetime as dt

from . import store


def raise_alert(key, text, grace_min=60, remind_h=24, link=""):
    open_ = store.kv_get("alerts_open", {})
    now = store.now()
    a = open_.get(key) or {"first": store.iso(now), "announced": None}
    a["text"] = text
    first = dt.datetime.fromisoformat(a["first"])
    due = (now - first).total_seconds() >= grace_min * 60
    last = dt.datetime.fromisoformat(a["announced"]) if a["announced"] else None
    if due and (last is None or (now - last).total_seconds() >= remind_h * 3600):
        store.log(f"ALERT {text}", "alert")
        a["announced"] = store.iso(now)
    open_[key] = a
    store.kv_set("alerts_open", open_)


def clear(key, text=None, quiet=False):
    open_ = store.kv_get("alerts_open", {})
    a = open_.pop(key, None)
    if a is None:
        return
    store.kv_set("alerts_open", open_)
    if a.get("announced") and not quiet:
        store.log(f"Resolved: {text or a['text']}", "info")


def open_alerts():
    return store.kv_get("alerts_open", {})


def due_alerts():
    """Alerts that have outlasted their grace period (what the daily check should report)."""
    return {k: a for k, a in open_alerts().items() if a.get("announced")}
