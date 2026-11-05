"""Alerts to the rev ops Google Chat space (Slack as fallback), quiet by design (same rules as the List Desk): a problem is a condition re-checked every
pass; it is announced only after it has lasted `grace_min`, repeated every `remind_h` while it lasts,
and gets one 'resolved' line if it was announced. Routine activity never leaves the app."""
import datetime as dt
import html

import requests

from . import config, store


def _url(link):
    return config.PUBLIC_URL.rstrip("/") + (link or "") if config.PUBLIC_URL else ""


def _post(text, kind="alert", link=""):
    """Google Chat (rev ops space) when GOOGLE_CHAT_WEBHOOK is set, else Slack, else nothing."""
    url = _url(link)
    try:
        if config.GOOGLE_CHAT_WEBHOOK:
            widgets = [{"textParagraph": {"text": html.escape(text)}}]
            if url:
                widgets.append({"buttonList": {"buttons": [{"text": "Open the Scrape Desk", "onClick": {"openLink": {"url": url}}}]}})
            card = {"header": {"title": "Scrape Desk", "subtitle": "Resolved" if kind == "resolved" else "Needs attention"},
                    "sections": [{"widgets": widgets}]}
            return requests.post(config.GOOGLE_CHAT_WEBHOOK, json={"cardsV2": [{"cardId": "desk", "card": card}]}, timeout=20).ok
        if config.SLACK_ALERT_WEBHOOK:
            icon = ":large_green_circle:" if kind == "resolved" else ":red_circle:"
            tail = f"\n<{url}|Open the Scrape Desk>" if url else ""
            return requests.post(config.SLACK_ALERT_WEBHOOK, json={"text": f"{icon} *Scrape Desk:* {text}{tail}"}, timeout=20).ok
    except Exception:
        return False
    return False


def raise_alert(key, text, grace_min=60, remind_h=24, link=""):
    open_ = store.kv_get("alerts_open", {})
    now = store.now()
    a = open_.get(key) or {"first": store.iso(now), "announced": None}
    a["text"] = text
    first = dt.datetime.fromisoformat(a["first"])
    due = (now - first).total_seconds() >= grace_min * 60
    last = dt.datetime.fromisoformat(a["announced"]) if a["announced"] else None
    if due and (last is None or (now - last).total_seconds() >= remind_h * 3600):
        _post(text, "alert", link)
        a["announced"] = store.iso(now)
        store.log(f"ALERT {text}", "alert")
    open_[key] = a
    store.kv_set("alerts_open", open_)


def clear(key, text=None, quiet=False):
    open_ = store.kv_get("alerts_open", {})
    a = open_.pop(key, None)
    if a is None:
        return
    store.kv_set("alerts_open", open_)
    if a.get("announced") and not quiet:
        _post(f"Resolved: {text or a['text']}", "resolved")


def open_alerts():
    return store.kv_get("alerts_open", {})
