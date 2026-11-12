"""Inbox Health: lifetime bounce rate per sending domain, read from EmailBison (sender emails) and cross-referenced
with Hypertide (orders). Read-only for now: it watches and alerts; nothing is paused, bought or swapped here.

Each domain also gets a 0-100 health score and zone (health.py), computed from 7/14-day windows where there is
enough history, else lifetime.

Lifetime rate = bounced / sent, added up over every mailbox on a domain, judged once the domain has
`inbox_min_sends` sends. Bison's own bands: under 2% fine, 2-3% warning, 3%+ critical. Whole domains are judged,
not single mailboxes, because Hypertide replaces by domain and one mailbox's few sends are too noisy."""
import datetime as dt
import threading
import time
import traceback
from zoneinfo import ZoneInfo

import requests

from . import alerts, config, health, store

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


def fetch_warmup():
    """{sender id: warm-up score} from Bison's warm-up listing (Bison's own 0-100 placement-style score)."""
    base, h = config.BISON_URL.rstrip("/"), {"Authorization": "Bearer " + config.BISON_KEY_VICTIG, "Accept": "application/json"}
    out, page = {}, 1
    while True:
        for attempt in range(5):
            r = requests.get(f"{base}/api/warmup/sender-emails", params={"page": page}, headers=h, timeout=40)
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Bison warm-up listing kept answering 429")
        j = r.json()
        for x in j["data"]:
            if x.get("warmup_score") is not None:
                out[x["id"]] = x["warmup_score"]
        if page >= j["meta"]["last_page"]:
            return out
        page += 1
        time.sleep(0.1)


def _doh(name, rtype):
    r = requests.get("https://cloudflare-dns.com/dns-query", params={"name": name, "type": rtype},
                     headers={"Accept": "application/dns-json"}, timeout=15)
    r.raise_for_status()
    return [a.get("data", "") for a in r.json().get("Answer", []) or []]


def fetch_dns(domain):
    """Public DNS check for one domain: SPF, DMARC, MX, and DKIM at the usual selectors (Microsoft selector1,
    Google, default). Returns booleans, or {"error": ...} when DNS could not be read (that is not a fail)."""
    try:
        txt = [t.strip('"').lower() for t in _doh(domain, "TXT")]
        dm = [t.strip('"').lower() for t in _doh("_dmarc." + domain, "TXT")]
        dkim = bool(_doh("selector1._domainkey." + domain, "CNAME")) or any(
            "v=dkim1" in t.lower() for sel in ("google", "default") for t in _doh(f"{sel}._domainkey.{domain}", "TXT"))
        return {"spf": any(t.startswith("v=spf1") for t in txt), "dmarc": any(t.startswith("v=dmarc1") for t in dm),
                "mx": bool(_doh(domain, "MX")), "dkim": dkim}
    except Exception as e:
        return {"error": str(e)[:80]}


def refresh_dns(domains, max_age_h=24):
    """Re-check each domain's DNS at most once a day; keeps the last good answer if a check errors."""
    cur = store.kv_get("inbox_dns") or {}
    cut = (store.now() - dt.timedelta(hours=max_age_h)).isoformat(timespec="seconds")
    for d in domains:
        if d in cur and not cur[d].get("error") and cur[d].get("at", "") > cut:
            continue
        r = fetch_dns(d)
        if r.get("error") and d in cur and not cur[d].get("error"):
            continue
        cur[d] = {**r, "at": store.iso()}
        time.sleep(0.05)
    store.kv_set("inbox_dns", {d: v for d, v in cur.items() if d in set(domains)})


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
        try:                                 # warm-up scores are a bonus signal: Bison hiccups must not block the sync
            wu = fetch_warmup()
        except Exception as e:
            wu = {}
            store.log(f"inbox: warm-up scores unavailable: {e}", "warn")
        rows = [(s["id"], s["email"].lower(), s["email"].split("@")[-1].lower(), s.get("status"),
                 1 if s.get("warmup_enabled") else 0, s.get("daily_limit") or 0, s.get("emails_sent_count") or 0,
                 s.get("bounced_count") or 0, s.get("total_replied_count") or 0,
                 s.get("interested_leads_count") or 0, wu.get(s["id"])) for s in senders]
        if len(rows) < 10:
            raise RuntimeError(f"Bison returned only {len(rows)} sender emails; keeping the previous data")
        store.x("DELETE FROM inbox_senders")
        store.xmany("INSERT INTO inbox_senders(id,email,domain,status,warmup,daily_limit,sent,bounced,replied,interested,warmup_score) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        if config.HYPERTIDE_API_KEY:
            try:
                store.kv_set("hypertide_orders", fetch_hypertide())
            except Exception as e:           # Hypertide being down must not hide the Bison data
                store.log(f"inbox: Hypertide lookup failed: {e}", "warn")
        try:
            refresh_dns(sorted({r[2] for r in rows}))
        except Exception as e:
            store.log(f"inbox: DNS checks failed: {e}", "warn")
        snapshot()
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


def today():
    return dt.datetime.now(ZoneInfo(config.TZ)).date().isoformat()


def snapshot():
    """One row per domain per day (the latest sync of the day wins): lifetime counters for trends and the 7/14-day
    windows. The day's health score and zone are filled in afterwards by `overview()` via `_store_scores`."""
    day = today()
    store.xmany("INSERT OR REPLACE INTO inbox_history(day,domain,sent,bounced,mailboxes,connected,replied,interested) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(day, d["domain"], d["sent"], d["bounced"], d["mailboxes"], d["connected"] or 0, d["replied"] or 0,
                  d["interested"] or 0) for d in store.q(
                    "SELECT domain, COUNT(*) mailboxes, SUM(status='Connected') connected, SUM(sent) sent, "
                    "SUM(bounced) bounced, SUM(replied) replied, SUM(interested) interested "
                    "FROM inbox_senders GROUP BY domain")])


def _store_scores(doms):
    day = today()
    store.xmany("UPDATE inbox_history SET score=?, zone=? WHERE day=? AND domain=?",
                [(d["health"]["score"], d["health"]["zone"], day, d["domain"]) for d in doms if d.get("health")])


def _window(rows, d, days, s):
    """Sends/bounces/replies over roughly the last `days` days: today's lifetime counters minus the newest snapshot
    that is at least `days` old (and not absurdly older). None if there is no such snapshot, it predates the replied
    column, the counters went backwards (Bison reset), or the window has too few sends to trust."""
    want = (dt.date.fromisoformat(today()) - dt.timedelta(days=days)).isoformat()
    floor = (dt.date.fromisoformat(today()) - dt.timedelta(days=days + 4)).isoformat()
    old = [r for r in rows if floor <= r["day"] <= want and r.get("replied") is not None]
    if not old:
        return None
    o = old[-1]
    w = {k: (d[k] or 0) - (o[k] or 0) for k in ("sent", "bounced", "replied", "interested")}
    if min(w.values()) < 0 or w["sent"] < s["inbox_window_min_sends"]:
        return None
    w["days"] = (dt.date.fromisoformat(today()) - dt.date.fromisoformat(o["day"])).days
    return w


def _trends(doms, s):
    """Per domain from the daily snapshots: sparkline of the lifetime rate, the change over about 7 days, and the
    rate of just the sends in that window (shows whether new sends bounce more than the lifetime average)."""
    cut = (dt.date.fromisoformat(today()) - dt.timedelta(days=35)).isoformat()
    hist = {}
    for r in store.q("SELECT day, domain, sent, bounced FROM inbox_history WHERE day>=? ORDER BY day", (cut,)):
        hist.setdefault(r["domain"], []).append(r)
    want = (dt.date.fromisoformat(today()) - dt.timedelta(days=7)).isoformat()
    for d in doms:
        pts = hist.get(d["domain"], [])
        d["spark"] = [round(p["bounced"] / p["sent"] * 100, 2) for p in pts if p["sent"] >= s["inbox_min_sends"]][-30:]
        d["trend"] = d["recent_rate"] = None
        old = [p for p in pts if p["day"] <= want] or ([pts[0]] if pts and pts[0]["day"] < today() else [])
        if old and d["rate"] is not None and old[-1]["sent"] >= s["inbox_min_sends"]:
            o = old[-1]
            days = (dt.date.fromisoformat(today()) - dt.date.fromisoformat(o["day"])).days
            d["trend"] = {"pp": round(d["rate"] - o["bounced"] / o["sent"] * 100, 2), "days": days}
            if d["sent"] - o["sent"] >= 50:
                d["recent_rate"] = round((d["bounced"] - o["bounced"]) / (d["sent"] - o["sent"]) * 100, 2)


# ---- reading -------------------------------------------------------------------------------------

def band(rate, s=None):
    s = s or store.settings()
    if rate is None:
        return "new"
    return "critical" if rate >= s["inbox_crit_pct"] else "warn" if rate >= s["inbox_warn_pct"] else "ok"


def reply_band(rate, s=None):
    """Reply rate is the headline health signal: a LOW rate is bad (the opposite of bounce)."""
    s = s or store.settings()
    if rate is None:
        return "new"
    return "critical" if rate < s["inbox_reply_crit_pct"] else "warn" if rate < s["inbox_reply_warn_pct"] else "ok"


def _health(doms, s):
    """Attach the 0-100 health score to each domain (see health.py). Windows prefer the last 7 days, then 14, then
    lifetime; the fleet baseline for reply rate is the whole fleet's lifetime rates."""
    tot = store.q1("SELECT COALESCE(SUM(sent),0) sent, COALESCE(SUM(replied),0) replied, COALESCE(SUM(interested),0) i "
                   "FROM inbox_senders")
    fleet = {"reply_rate": tot["replied"] / tot["sent"] * 100 if tot["sent"] else None,
             "positive_rate": tot["i"] / tot["sent"] * 100 if tot["sent"] else None}
    cut = (dt.date.fromisoformat(today()) - dt.timedelta(days=40)).isoformat()
    hist = {}
    for r in store.q("SELECT day, domain, sent, bounced, replied, interested, score FROM inbox_history WHERE day>=? ORDER BY day", (cut,)):
        hist.setdefault(r["domain"], []).append(r)
    dns = store.kv_get("inbox_dns") or {}
    for d in doms:
        rows = hist.get(d["domain"], [])
        w7, w14 = _window(rows, d, 7, s), _window(rows, d, 14, s)
        w, basis = (w7, "7d") if w7 else (w14, "14d") if w14 else (None, "lifetime")
        if w is None and d["sent"] < s["inbox_min_sends"]:
            d["health"] = {"score": None, "zone": "new", "basis": None, "coverage": 0, "drag": None, "factors": {},
                           "hard_fail": [], "capped": []}
            continue
        m = w or {k: d[k] or 0 for k in ("sent", "bounced", "replied", "interested")}
        util = m["sent"] / (d["limit_sum"] * w["days"]) if w and d["limit_sum"] else None
        d["health"] = health.score_domain({**m, "basis": basis, "mailboxes": d["mailboxes"], "util": util,
                                           "warmup": d["warmup_avg"], "dns": dns.get(d["domain"])},
                                          fleet, s.get("health_weights"))
        d["dns"] = {k: dns[d["domain"]].get(k) for k in ("spf", "dkim", "dmarc", "mx", "error")} if d["domain"] in dns else None
        want = (dt.date.fromisoformat(today()) - dt.timedelta(days=7)).isoformat()
        old = [r for r in rows if r["day"] <= want and r.get("score") is not None]
        sc = d["health"]["score"]
        d["health"]["trend"] = round(sc - old[-1]["score"], 1) if old and sc is not None else None


def overview():
    s = store.settings()
    ht = store.kv_get("hypertide_orders") or {}
    doms = []
    for d in store.q("SELECT domain, COUNT(*) mailboxes, SUM(status='Connected') connected, SUM(warmup) warmup, "
                     "SUM(sent) sent, SUM(bounced) bounced, SUM(replied) replied, SUM(interested) interested, "
                     "SUM(CASE WHEN status='Connected' THEN daily_limit ELSE 0 END) limit_sum, "
                     "AVG(warmup_score) warmup_avg FROM inbox_senders GROUP BY domain"):
        rate = round(d["bounced"] / d["sent"] * 100, 2) if d["sent"] >= s["inbox_min_sends"] else None
        o = ht.get(d["domain"], {})
        rr = round((d["replied"] or 0) / d["sent"] * 100, 2) if d["sent"] >= s["inbox_reply_min_sends"] else None
        doms.append({**d, "rate": rate, "band": band(rate, s), "reply_rate": rr, "reply_band": reply_band(rr, s),
                     "ht_status": o.get("status"), "created": o.get("created")})
    _trends(doms, s)
    _health(doms, s)
    # worst health first (domains with too few sends last), then lowest reply rate
    doms.sort(key=lambda x: (x["health"]["score"] is None, x["health"]["score"] if x["health"]["score"] is not None else 0,
                             x["reply_rate"] if x["reply_rate"] is not None else 0, x["domain"]))
    tot = store.q1("SELECT COUNT(*) n, COALESCE(SUM(sent),0) sent, COALESCE(SUM(bounced),0) bounced, "
                   "COALESCE(SUM(status!='Connected'),0) disconnected FROM inbox_senders")
    counts = {b: sum(1 for x in doms if x["band"] == b) for b in ("critical", "warn", "ok", "new")}
    reply_counts = {b: sum(1 for x in doms if x["reply_band"] == b) for b in ("critical", "warn", "ok", "new")}
    tot_replied = store.q1("SELECT COALESCE(SUM(replied),0) r FROM inbox_senders")["r"]
    zones = {z: sum(1 for x in doms if x["health"]["zone"] == z) for z in ("healthy", "watch", "danger", "retire", "new")}
    return {"domains": doms, "counts": counts, "zones": zones, "weights": {**health.DEFAULT_WEIGHTS, **(s.get("health_weights") or {})}, "mailboxes": tot["n"], "disconnected": tot["disconnected"],
            "sent": tot["sent"], "bounced": tot["bounced"],
            "rate": round(tot["bounced"] / tot["sent"] * 100, 2) if tot["sent"] else None,
            "reply_rate": round(tot_replied / tot["sent"] * 100, 2) if tot["sent"] else None, "reply_counts": reply_counts,
            "reply_warn_pct": s["inbox_reply_warn_pct"], "reply_crit_pct": s["inbox_reply_crit_pct"],
            "reply_min_sends": s["inbox_reply_min_sends"],
            "sync": store.kv_get("inbox_sync"), "configured": bool(config.BISON_KEY_VICTIG),
            "hypertide": bool(config.HYPERTIDE_API_KEY), "min_sends": s["inbox_min_sends"], "window_min_sends": s["inbox_window_min_sends"],
            "warn_pct": s["inbox_warn_pct"], "crit_pct": s["inbox_crit_pct"],
            "alerts": [a["text"] for k, a in alerts.open_alerts().items() if k.startswith("inbox:")]}


def check():
    """Keeps today's score history up to date. It raises no per-domain alerts any more: a burnt domain is the
    rotation's job to fix, and the team is only told when the automation itself breaks (`inbox:sync`, rotation
    errors) plus the weekly heartbeat. Old per-domain alerts left over from earlier versions are cleared."""
    o = overview()
    _store_scores(o["domains"])
    for k in [k for k in alerts.open_alerts() if k.startswith("inbox:") and k != "inbox:sync" and not k.startswith("inbox:rotation")]:
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
