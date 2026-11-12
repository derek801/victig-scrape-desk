"""Automatic rotation: when a domain's health lands in the Retire zone, swap a warmed bench domain in, take the
retired one off its campaigns, and note that the bench needs refilling.

Modes (setting `rotation_mode`):  off | dry (default: plans and logs, changes nothing) | live (really calls Bison).
Guardrails: a daily swap cap, a mass-retire stop (many domains failing at once usually means a bad list or bad copy,
not burnt domains, so nothing is swapped and the team is told), the replacement must be warm AND have healthy DNS
(re-checked at swap time), the new mailboxes go onto each campaign BEFORE the old ones come off (no gap, leads stay
with the campaign), and every step is written to `rotation_log`.

What stays with a person (shown on the page and in the weekly heartbeat): buying new bench domains (the Hypertide
quote/checkout schema is not verified, so no money moves here), cancelling or decommissioning a retired Hypertide
subscription, registrar nameserver changes for domains Hypertide does not manage.

The executors (`Bison`, `Hypertide`) are small classes so tests can swap in fakes; this module never talks to Chat."""
import datetime as dt
import json
import time

import requests

from . import alerts, config, inbox, ordering, store

NEEDS_PERSON = ["Buy new bench domains: the desk can order them, but only when bench_autobuy is switched on and rotation is live "
                "(until then a person orders them in Hypertide)", "Cancel or decommission a retired domain's Hypertide subscription",
                "Registrar nameserver changes for domains Hypertide does not manage"]
WARM_MIN = 85                      # average Bison warm-up score a replacement's mailboxes need
ERR_KEY = "inbox:rotation:error"
MASS_KEY = "inbox:rotation:mass"
EMPTY_KEY = "inbox:rotation:bench-empty"


# ---- executors -----------------------------------------------------------------------------------

class Bison:
    def __init__(self):
        self.base = config.BISON_URL.rstrip("/")
        self.h = {"Authorization": "Bearer " + config.BISON_KEY_VICTIG, "Accept": "application/json"}

    def campaigns_of(self, sender_id):
        out, page = [], 1
        while True:
            r = requests.get(f"{self.base}/api/sender-emails/{sender_id}/campaigns", params={"page": page}, headers=self.h, timeout=40)
            r.raise_for_status()
            j = r.json()
            out += [c["id"] for c in j["data"] if c.get("status") not in ("completed", "archived")]
            if page >= j["meta"]["last_page"]:
                return out
            page += 1

    def _post(self, cid, path, ids):
        r = requests.post(f"{self.base}/api/campaigns/{cid}/{path}", json={"sender_email_ids": ids}, headers=self.h, timeout=40)
        r.raise_for_status()

    def attach(self, cid, ids):
        self._post(cid, "attach-sender-emails", ids)

    def remove(self, cid, ids):
        self._post(cid, "remove-sender-emails", ids)


class Hypertide:
    def pool(self):
        """Recovery ("Backup") pool: [{domain, status, score, eligible_at}]."""
        out, after = [], None
        while True:
            r = requests.get("https://backend.hypertide.io/api/v1/warmup-domain-pool",
                             params={"limit": 150, **({"afterDomain": after} if after else {})},
                             headers={"X-API-Key": config.HYPERTIDE_API_KEY}, timeout=60)
            r.raise_for_status()
            j = r.json()
            for d in j.get("data", []):
                rh = d.get("recoveryHealth") or {}
                out.append({"domain": (d.get("domain") or "").lower(), "status": d.get("status"), "score": rh.get("score"),
                            "eligible_at": rh.get("eligibleAt")})
            if not j.get("hasMore"):
                return out
            after = j.get("nextCursor")
            time.sleep(2)


# ---- logging and state ---------------------------------------------------------------------------

def log(run, mode, step, domain, ok, detail):
    store.x("INSERT INTO rotation_log(ts,run,mode,step,domain,ok,detail) VALUES(?,?,?,?,?,?,?)",
            (store.iso(), run, mode, step, domain, 1 if ok else 0, json.dumps(detail)[:1500]))


def lifecycle():
    return store.kv_get("domain_lifecycle") or {}


def swaps_today():
    day = store.iso()[:10]
    return store.q1("SELECT COUNT(*) n FROM rotation_log WHERE step='swap' AND mode='live' AND ok=1 AND ts>=?", (day,))["n"]


# ---- bench ---------------------------------------------------------------------------------------

def bench(hyper=None, fresh_dns=False):
    """Bench candidates from Hypertide's pool, each marked ready or not (with the reason)."""
    s = store.settings()
    hyper = hyper or Hypertide()
    senders = {}
    for r in store.q("SELECT id, domain, status, warmup_score FROM inbox_senders"):
        senders.setdefault(r["domain"], []).append(r)
    dns = store.kv_get("inbox_dns") or {}
    out = []
    base = [{**p, "source": "pool"} for p in hyper.pool() if p["status"] != "cancelled"]
    seen = {p["domain"] for p in base}
    for o in store.kv_get("bench_orders") or []:         # domains the desk ordered itself (not in the recovery pool)
        if o["domain"] in seen:
            continue
        age = (store.now() - dt.datetime.fromisoformat(o["at"])).days
        st = "payment_pending" if not o.get("paid") else "ready" if age >= s["bench_min_days"] else "warming_up"
        base.append({"domain": o["domain"], "status": st, "score": None, "eligible_at": None, "source": "ordered", "age": age})
    for p in base:
        rows = [x for x in senders.get(p["domain"], []) if x["status"] == "Connected"]
        scores = [x["warmup_score"] for x in rows if x["warmup_score"] is not None]
        warm = sum(scores) / len(scores) if scores else None
        d = fresh_dns and inbox.fetch_dns(p["domain"]) or dns.get(p["domain"]) or {}
        dns_ok = bool(d.get("spf") and d.get("dmarc") and d.get("mx") and not d.get("error"))
        reason = None
        if p["status"] != "ready":
            reason = "still " + str(p["status"]).replace("_", " ") + (f" (ordered {p['age']} days ago)" if p.get("age") is not None else "")
        elif p["source"] == "pool" and (p["score"] is None or p["score"] < s["bench_min_score"]):
            reason = f"recovery score {p['score']} under {s['bench_min_score']}"
        elif not rows:
            reason = "no connected mailboxes in Bison yet"
        elif warm is None or warm < WARM_MIN:
            reason = f"warm-up score {None if warm is None else round(warm)} under {WARM_MIN}"
        elif not dns_ok:
            reason = "DNS not verified (SPF, DMARC, MX)"
        out.append({**p, "mailboxes": len(rows), "warmup": None if warm is None else round(warm), "dns_ok": dns_ok,
                    "ready": reason is None, "reason": reason, "sender_ids": [x["id"] for x in rows]})
    return out


def bench_status(hyper=None):
    s = store.settings()
    try:
        b = bench(hyper)
        err = None
    except Exception as e:
        b, err = [], str(e)[:150]
    ready = [x for x in b if x["ready"]]
    return {"target": s["bench_target"], "ready": len(ready), "candidates": b, "error": err,
            "short_by": max(0, s["bench_target"] - len(ready)), "needs_person": NEEDS_PERSON, "mode": s["rotation_mode"],
            "daily_cap": s["rotation_daily_cap"], "swaps_today": swaps_today()}


# ---- the cycle -----------------------------------------------------------------------------------

def run_cycle(bison=None, hyper=None):
    """One pass: find Retire-zone domains, swap bench domains in (up to the daily cap). Never raises."""
    s = store.settings()
    mode = s["rotation_mode"]
    if mode == "off":
        return {"mode": "off"}
    live = mode == "live"
    run = store.iso()
    result = {"mode": mode, "swaps": [], "skipped": None}
    try:
        o = inbox.overview()
        life = lifecycle()
        retire = [d for d in o["domains"] if d["health"]["zone"] == "retire"
                  and life.get(d["domain"], {}).get("state") not in ("resting", "decommissioned")]
        if len(retire) > s["rotation_mass_limit"]:
            alerts.raise_alert(MASS_KEY, f"{len(retire)} domains hit Retire at once, so rotation stopped (that pattern usually means a bad "
                               "list or bad copy, not burnt domains). Nothing was swapped.", grace_min=0, remind_h=24, link="/#inbox")
            log(run, mode, "mass_stop", None, False, {"retire": len(retire), "limit": s["rotation_mass_limit"]})
            result["skipped"] = "mass"
            return result
        alerts.clear(MASS_KEY)
        allb = bench(hyper, fresh_dns=bool(retire))
        b = [x for x in allb if x["ready"]]
        store.kv_set("bench_ready", {"n": len(b), "at": store.iso()})
        if not retire:
            alerts.clear(EMPTY_KEY)
            if len(b) < s["bench_target"]:
                ordering.top_up(run, live, allb)
            _finish_ok(run, mode)
            return result
        room = s["rotation_daily_cap"] - swaps_today()
        if room <= 0:
            log(run, mode, "cap_reached", None, True, {"retire": [d["domain"] for d in retire]})
            result["skipped"] = "cap"
            _finish_ok(run, mode)
            return result
        bison = bison or Bison()
        for d in sorted(retire, key=lambda x: x["health"]["score"] if x["health"]["score"] is not None else 0)[:room]:
            if not b:
                alerts.raise_alert(EMPTY_KEY, f"{d['domain']} needs replacing but the bench has no ready domain. A person needs to order "
                                   "more bench domains.", grace_min=0, remind_h=24, link="/#inbox")
                log(run, mode, "bench_empty", d["domain"], False, {})
                result["skipped"] = "bench-empty"
                break
            alerts.clear(EMPTY_KEY)
            repl = b.pop(0)
            result["swaps"].append(_swap(run, live, bison, d, repl, life))
        left = len(b)
        if left < s["bench_target"]:
            log(run, mode, "refill_needed", None, True, {"ready_left": left, "target": s["bench_target"]})
            ordering.top_up(run, live, allb)
        _finish_ok(run, mode)
    except Exception as e:
        store.log("rotation error: " + str(e)[:200], "error")
        log(run, mode, "error", None, False, {"error": str(e)[:300]})
        alerts.raise_alert(ERR_KEY, f"Domain rotation hit an error: {str(e)[:150]}", grace_min=0, remind_h=24, link="/#inbox")
        result["skipped"] = "error"
    return result


def _finish_ok(run, mode):
    if ERR_KEY in alerts.open_alerts():
        alerts.clear(ERR_KEY, "Domain rotation recovered")
        log(run, mode, "recovered", None, True, {})


def _swap(run, live, bison, d, repl, life):
    """Attach the replacement's mailboxes to every campaign the retiring domain's mailboxes are on, then take the old
    ones off. Dry mode does the reads (campaign lookup) but none of the writes."""
    mode = "live" if live else "dry"
    old_ids = [r["id"] for r in store.q("SELECT id FROM inbox_senders WHERE domain=?", (d["domain"],))]
    camps = {}
    for sid in old_ids:
        for cid in bison.campaigns_of(sid):
            camps.setdefault(cid, []).append(sid)
    new_ids = repl["sender_ids"]
    plan = []
    for cid, olds in camps.items():
        add = new_ids[:max(1, len(olds))]
        plan.append({"campaign": cid, "attach": add, "remove": olds})
    log(run, mode, "plan", d["domain"], True, {"retire": d["domain"], "score": d["health"]["score"],
                                              "reasons": d["health"]["hard_fail"] or [d["health"]["drag"]], "replacement": repl["domain"],
                                              "campaigns": len(plan)})
    if live:
        for p in plan:                                  # attach first, remove second: no gap, leads stay with the campaign
            bison.attach(p["campaign"], p["attach"])
            log(run, mode, "attach", d["domain"], True, {"campaign": p["campaign"], "ids": p["attach"]})
        for p in plan:
            bison.remove(p["campaign"], p["remove"])
            log(run, mode, "remove", d["domain"], True, {"campaign": p["campaign"], "ids": p["remove"]})
        life[d["domain"]] = {"state": "resting", "since": store.iso(), "replaced_by": repl["domain"]}
        life[repl["domain"]] = {"state": "active", "since": store.iso()}
        store.kv_set("domain_lifecycle", life)
    log(run, mode, "swap", d["domain"], True, {"out": d["domain"], "in": repl["domain"], "campaigns": len(plan),
                                              "note": "no changes made (dry run)" if not live else "done"})
    return {"out": d["domain"], "in": repl["domain"], "campaigns": len(plan), "mode": mode}


# ---- reporting -----------------------------------------------------------------------------------

def heartbeat(days=7):
    cut = (store.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
    c = {r["k"]: r["n"] for r in store.q("SELECT step||':'||mode k, COUNT(*) n FROM rotation_log WHERE ts>=? AND ok=1 GROUP BY k", (cut,))}
    errs = store.q1("SELECT COUNT(*) n FROM rotation_log WHERE ts>=? AND step='error'", (cut,))["n"]
    rec = store.q1("SELECT COUNT(*) n FROM rotation_log WHERE ts>=? AND step='recovered'", (cut,))["n"]
    last = store.q1("SELECT MAX(ts) t FROM rotation_log")["t"]
    s = store.settings()
    return {"days": days, "mode": s["rotation_mode"], "swaps_live": c.get("swap:live", 0), "swaps_planned_dry": c.get("swap:dry", 0),
            "errors": errs, "errors_recovered": rec, "refills_needed": c.get("refill_needed:live", 0) + c.get("refill_needed:dry", 0),
            "bench_target": s["bench_target"], "bench_ready": (store.kv_get("bench_ready") or {}).get("n"),
            "last_activity": last}


def summary(days=7):
    """One plain sentence for the weekly heartbeat line in the daily Make check."""
    h = heartbeat(days)
    mode = {"off": "off", "dry": "dry run (plans only)", "live": "live"}[h["mode"]]
    swaps = f"{h['swaps_live']} swaps" if h["mode"] == "live" else f"{h['swaps_planned_dry']} swaps planned, none made"
    bench = "unknown" if h["bench_ready"] is None else f"{h['bench_ready']}/{h['bench_target']}"
    return (f"Inbox rotation ({mode}), last {days} days: {swaps}; bench ready {bench}; "
            f"{h['errors']} errors, {h['errors_recovered']} recovered.")


def recent_log(n=40):
    rows = store.q("SELECT ts, mode, step, domain, ok, detail FROM rotation_log ORDER BY id DESC LIMIT ?", (n,))
    for r in rows:
        r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
    return rows
