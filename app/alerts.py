"""Slack alerts, quiet by design (same rules as the List Desk): a problem is a condition re-checked every
pass; it is announced only after it has lasted `grace_min`, repeated every `remind_h` while it lasts,
and gets one 'resolved' line if it was announced. Routine activity never leaves the app."""
import datetime as dt

import requests

from . import config, store


def _post(text):
    if not config.SLACK_ALERT_WEBHOOK:
        return False
    try:
        return requests.post(config.SLACK_ALERT_WEBHOOK, json={"text": text}, timeout=20).ok
    except Exception:
        return False


def _link():
    return f"\n<{config.PUBLIC_URL}|Open the Scrape Desk>" if config.PUBLIC_URL else ""


def raise_alert(key, text, grace_min=60, remind_h=24):
    open_ = store.kv_get("alerts_open", {})
    now = store.now()
    a = open_.get(key) or {"first": store.iso(now), "announced": None}
    a["text"] = text
    first = dt.datetime.fromisoformat(a["first"])
    due = (now - first).total_seconds() >= grace_min * 60
    last = dt.datetime.fromisoformat(a["announced"]) if a["announced"] else None
    if due and (last is None or (now - last).total_seconds() >= remind_h * 3600):
        _post(f":red_circle: *Scrape Desk:* {text}{_link()}")
        a["announced"] = store.iso(now)
        store.log(f"ALERT {text}", "alert")
    open_[key] = a
    store.kv_set("alerts_open", open_)


def clear(key, text=None):
    open_ = store.kv_get("alerts_open", {})
    a = open_.pop(key, None)
    if a is None:
        return
    store.kv_set("alerts_open", open_)
    if a.get("announced"):
        _post(f":large_green_circle: *Scrape Desk:* resolved: {text or a['text']}")


def open_alerts():
    return store.kv_get("alerts_open", {})
