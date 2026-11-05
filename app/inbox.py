"""Inbox Health: lifetime bounce rate per sending domain, read from EmailBison (sender emails) and cross-referenced
with Hypertide (orders). Read-only for now: it watches and alerts; nothing is paused, bought or swapped here.

Lifetime rate = bounced / sent, added up over every mailbox on a domain, judged once the domain has
`inbox_min_sends` sends. Bison's own bands: under 2% fine, 2-3% warning, 3%+ critical. Whole domains are judged,
not single mailboxes, because Hypertide replaces by domain and one mailbox's few sends are too noisy."""
import threading
import time
import traceback

import requests

from . import alerts, config, store

SYNC_EVERY_S = 6 * 3600
MAX_CRIT_ALERTS = 5          # more critical domains than this at once = one summary, not one message each
_running = threading.Lock()


# ---- fetching ------------------------------------------------------------------------------------

def _bison_pages():
    """Every sender email from Bison, 15 per page. Yields lists of records."""
    base, h = config.BISON_URL.rstrip("/"), {"Authorization": "Bearer " + config.BISON_KEY_VICTIG, "Accept": "application/json"}
    page = 1
    while True:
        for attempt in range(5):
            r = requests.get(f"{base}/api/sender-emails", params={"page": page}, headers=h, timeout=40)
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Bison kept answering 429")
        j = r.json()
        yield j["data"]
        if page >= j["meta"]["last_page"]:
            return
        page += 1
        time.sleep(0.1)


def fetch_senders():
    out = []
    for rows in _bison_pages():
        out += rows
    return out


def fetch_hypertide():
    """{domain: {status, created, tool}} from Hypertide's active orders (one call)."""
    r = requests.get("https://backend.hypertide.io/api/v1/orders/active",
                     headers={"X-API-Key": config.HYPERTIDE_API_KEY}, timeout=60)
    r.raise_for_status()
    return {o["domain"].lower(): {"status": o.get("status"), "created": (o.get("createdAt") or "")[:10],
                                  "tool": o.get("sendingTool")} for o in r.json().get("orders", [])}


# ---- storing -------------------------------------------------------------------------------------

def sync():
    """Pull everything, swap it into the database in one go, then re-check alerts. Never raises."""
    if not config.BISON_KEY_VICTIG or not _running.acquire(blocking=False):
        return False
    try:
        senders = fetch_senders()
        rows = [(s["id"], s["email"].lower(), s["email"].split("@")[-1].lower(), s.get("status"),
                 1 if s.get("warmup_enabled") else 0, s.get("daily_limit") or 0, s.get("emails_sent_count") or 0,
                 s.get("bounced_count") or 0, s.get("total_replied_count") or 0) for s in senders]
        if len(rows) < 10:
            raise RuntimeError(f"Bison returned only {len(rows)} sender emails; keeping the previous data")
        store.x("DELETE FROM inbox_senders")
        store.xmany("INSERT INTO inbox_senders(id,email,domain,status,warmup,daily_limit,sent,bounced,replied) "
                    "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        if config.HYPERTIDE_API_KEY:
            try:
                store.kv_set("hypertide_orders", fetch_hypertide())
            except Exception as e:           # Hypertide being down must not hide the Bison data
                store.log(f"inbox: Hypertide lookup failed: {e}", "warn")
        store.kv_set("inbox_sync", {"at": store.iso(), "senders": len(rows), "error": None})
        alerts.clear("inbox:sync")
        check()
        return True
    except Exception as e:
        prev = store.kv_get("inbox_sync") or {}
        store.kv_set("inbox_sync", {**prev, "error": str(e)[:200], "failed_at": store.iso()})
        store.log("inbox sync failed: " + str(e)[:200], "warn")
        alerts.raise_alert("inbox:sync", f"Inbox Health can't read EmailBison: {str(e)[:150]}", grace_min=120, link="/#inbox")
        return False
    finally:
        _running.release()


# ---- reading -------------------------------------------------------------------------------------

def band(rate, s=None):
    s = s or store.settings()
    if rate is None:
        return "new"
    return "critical" if rate >= s["inbox_crit_pct"] else "warn" if rate >= s["inbox_warn_pct"] else "ok"


def overview():
    s = store.settings()
    ht = store.kv_get("hypertide_orders") or {}
    doms = []
    for d in store.q("SELECT domain, COUNT(*) mailboxes, SUM(status='Connected') connected, SUM(warmup) warmup, "
                     "SUM(sent) sent, SUM(bounced) bounced, SUM(replied) replied FROM inbox_senders GROUP BY domain"):
        rate = round(d["bounced"] / d["sent"] * 100, 2) if d["sent"] >= s["inbox_min_sends"] else None
        o = ht.get(d["domain"], {})
        doms.append({**d, "rate": rate, "band": band(rate, s), "ht_status": o.get("status"), "created": o.get("created")})
    doms.sort(key=lambda x: (-(x["rate"] if x["rate"] is not None else -1), x["domain"]))
    tot = store.q1("SELECT COUNT(*) n, COALESCE(SUM(sent),0) sent, COALESCE(SUM(bounced),0) bounced, "
                   "COALESCE(SUM(status!='Connected'),0) disconnected FROM inbox_senders")
    counts = {b: sum(1 for x in doms if x["band"] == b) for b in ("critical", "warn", "ok", "new")}
    return {"domains": doms, "counts": counts, "mailboxes": tot["n"], "disconnected": tot["disconnected"],
            "sent": tot["sent"], "bounced": tot["bounced"],
            "rate": round(tot["bounced"] / tot["sent"] * 100, 2) if tot["sent"] else None,
            "sync": store.kv_get("inbox_sync"), "configured": bool(config.BISON_KEY_VICTIG),
            "hypertide": bool(config.HYPERTIDE_API_KEY), "min_sends": s["inbox_min_sends"],
            "warn_pct": s["inbox_warn_pct"], "crit_pct": s["inbox_crit_pct"]}


def check():
    """Alert rules (never one message per item): each critical domain gets its own alert unless more than
    MAX_CRIT_ALERTS are critical at once, which is a summary (that pattern usually means bad copy or a bad list,
    not burnt domains). Warnings and disconnected mailboxes are weekly digests."""
    o = overview()
    crit = [d for d in o["domains"] if d["band"] == "critical"]
    open_ = alerts.open_alerts()
    wanted = set()
    if len(crit) > MAX_CRIT_ALERTS:
        wanted.add("inbox:crit:many")
        alerts.raise_alert("inbox:crit:many", f"{len(crit)} sending domains are at a {o['crit_pct']:g}%+ lifetime bounce rate at once, "
                           "which usually means a bad list or copy rather than burnt domains. Worst: "
                           + ", ".join(f"{d['domain']} ({d['rate']:g}%)" for d in crit[:5]) + ".",
                           grace_min=0, remind_h=24, link="/#inbox")
    else:
        for d in crit:
            k = f"inbox:crit:{d['domain']}"
            wanted.add(k)
            alerts.raise_alert(k, f"{d['domain']} is critical: {d['rate']:g}% lifetime bounce rate "
                               f"({d['bounced']:,} of {d['sent']:,} sends, {d['mailboxes']} mailboxes). It needs replacing.",
                               grace_min=0, remind_h=24, link="/#inbox")
    warn = [d for d in o["domains"] if d["band"] == "warn"]
    if warn:
        wanted.add("inbox:warn")
        alerts.raise_alert("inbox:warn", f"{len(warn)} sending domains are in the {o['warn_pct']:g}-{o['crit_pct']:g}% warning zone. "
                           "Highest: " + ", ".join(f"{d['domain']} ({d['rate']:g}%)" for d in warn[:3]) + ".",
                           grace_min=0, remind_h=168, link="/#inbox")
    if o["disconnected"]:
        wanted.add("inbox:disconnected")
        alerts.raise_alert("inbox:disconnected", f"{o['disconnected']:,} of {o['mailboxes']:,} mailboxes are not connected in Bison.",
                           grace_min=0, remind_h=168, link="/#inbox")
    for k in [k for k in open_ if k.startswith("inbox:") and k != "inbox:sync" and k not in wanted]:
        alerts.clear(k)


# ---- background ----------------------------------------------------------------------------------

def loop():
    time.sleep(90)                   # let the scheduler settle after a deploy first
    while True:
        try:
            last = (store.kv_get("inbox_sync") or {}).get("at")
            import datetime as dt
            if not last or (store.now() - dt.datetime.fromisoformat(last)).total_seconds() >= SYNC_EVERY_S:
                sync()
        except Exception:
            store.log("inbox loop crashed: " + traceback.format_exc()[-400:], "error")
        time.sleep(600)


def start():
    if config.BISON_KEY_VICTIG:
        threading.Thread(target=loop, daemon=True, name="inbox").start()


def sync_in_background():
    threading.Thread(target=sync, daemon=True, name="inbox-sync").start()
