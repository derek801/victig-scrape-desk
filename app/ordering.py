"""Orders bench domains in Hypertide (Microsoft/Entra, 52 mailboxes each, wired into Bison) when the bench is short.

Safe by construction: it only ever tops the bench up to `bench_target` (counting domains already ordered or warming),
never more than `bench_order_max_per_day` a day, and only when `rotation_mode` is live AND `bench_autobuy` is on (both
default off). In dry mode it logs the names it would order. Orders are written down BEFORE they are paid, and an
unpaid order is retried, never re-created, so a crash cannot double-buy. Bison login details come from env vars
(BISON_LOGIN_EMAIL / BISON_LOGIN_PASSWORD / BISON_WORKSPACE); they are never logged or shown.

Names follow the existing fleet pattern: [prefix]victig[word][suffix].com, no hyphens, .com only."""
import datetime as dt
import hashlib
import itertools
import json

import requests

from . import config, store

PREFIX = ["", "get", "hello", "team", "go", "meet", "book", "try"]
WORD = ["hr", "hiring", "people", "talent", "screening", "background", "verification", "candidate", "recruiting", "onboarding"]
SUFFIX = ["team", "hub", "help", "group", "desk", "support", "center", "check", "services"]
API = "https://backend.hypertide.io/api/v1"


def taken():
    """Every domain name we already know about, so we never reuse one."""
    names = {r["domain"] for r in store.q("SELECT DISTINCT domain FROM inbox_senders")}
    names |= set((store.kv_get("hypertide_orders") or {}).keys())
    names |= {o["domain"] for o in store.kv_get("bench_orders") or []}
    names |= set(store.kv_get("bench_names_tried") or [])
    return names


def candidates(n, used):
    """n unused names, in a fixed pseudo-random order (so a retry does not shuffle differently)."""
    combos = [f"{p}victig{w}{s}.com" for p, w, s in itertools.product(PREFIX, WORD, SUFFIX)]
    combos.sort(key=lambda d: hashlib.sha1(d.encode()).hexdigest())
    return [d for d in combos if d not in used and len(d) <= 35][:n]


def _headers():
    return {"X-API-Key": config.HYPERTIDE_API_KEY, "Content-Type": "application/json"}


def _users(s):
    out = []
    for n in s["bench_sender_names"]:
        parts = str(n).split()
        if len(parts) >= 2:
            out.append({"first_name": parts[0], "last_name": " ".join(parts[1:])})
    return out


def missing_setup(s):
    gaps = []
    if not _users(s):
        gaps.append("sender names (bench_sender_names)")
    if not (config.BISON_LOGIN_EMAIL and config.BISON_LOGIN_PASSWORD and config.BISON_WORKSPACE):
        gaps.append("Bison login for Hypertide (BISON_LOGIN_EMAIL, BISON_LOGIN_PASSWORD, BISON_WORKSPACE in Render)")
    if not config.HYPERTIDE_API_KEY:
        gaps.append("Hypertide key")
    return gaps


def _body(s, domains):
    return {"plan": "entra", "domain_option": "purchase_domain_for_me", "domains": domains,
            "forwarding_domain": s["bench_forwarding_domain"], "client_name": s["bench_client_name"], "selected_tool": "bison",
            "tool_credentials": {"api_key": config.BISON_KEY_VICTIG, "username": config.BISON_LOGIN_EMAIL,
                                 "password": config.BISON_LOGIN_PASSWORD, "bison_url": config.BISON_URL,
                                 "workspace": config.BISON_WORKSPACE},
            "users": _users(s), "inbox_quantity": s["bench_inbox_quantity"],
            "warmup_setup": {"disabled": False, "settings": {"warmup_limit": 5}, "tags": ["bench"]}}


def create_bulk(body):
    r = requests.post(f"{API}/orders/bulk", json=body, headers=_headers(), timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"order refused ({r.status_code}): {r.text[:200].replace(config.BISON_LOGIN_PASSWORD or '~', '***')}")
    return r.json()["data"]


def charge(record_ids):
    r = requests.post(f"{API}/payments/charge", json={"recordIds": record_ids}, headers=_headers(), timeout=120)
    ok = r.status_code == 200 and (r.json().get("paymentStatus") or r.json().get("data", {}).get("paymentStatus")) in ("paid", "no_payment_required")
    return ok, r.status_code


def _log(run, live, step, ok, detail):
    from . import rotation
    rotation.log(run, "live" if live else "dry", step, None, ok, detail)


def top_up(run, live, candidates_now, create=None, pay=None):
    """Order enough bench domains to reach the target. `candidates_now` is rotation.bench() output (everything counts that is
    ready or on its way). Returns what happened, for tests."""
    s = store.settings()
    orders = store.kv_get("bench_orders") or []
    unpaid = [o for o in orders if not o.get("paid")]
    create, pay = create or create_bulk, pay or charge
    if unpaid:                                           # finish what was ordered before ordering anything new
        if live and s["bench_autobuy"]:
            ok, code = pay([o["record_id"] for o in unpaid])
            if ok:
                for o in orders:
                    o["paid"] = True
                store.kv_set("bench_orders", orders)
            _log(run, live, "order_charge", ok, {"domains": [o["domain"] for o in unpaid], "http": code})
        return {"retried": [o["domain"] for o in unpaid]}
    have = len([c for c in candidates_now if c["status"] != "cancelled"])
    today = store.iso()[:10]
    ordered_today = sum(1 for o in orders if o["at"][:10] == today)
    n = min(s["bench_target"] - have, s["bench_order_max_per_day"] - ordered_today)
    if n <= 0:
        return {"ordered": []}
    names = candidates(n, taken())
    gaps = missing_setup(s)
    if not (live and s["bench_autobuy"]) or gaps:
        _log(run, live, "order_needed", True, {"would_order": names, "autobuy": s["bench_autobuy"], "missing": gaps,
                                               "note": "a person orders these, or turn on bench_autobuy"})
        return {"ordered": [], "would_order": names, "missing": gaps}
    return place(run, live, names, n, create, pay)


def place(run, live, names, n, create=None, pay=None):
    """Create the order for `names` (swapping in the next unused names if one is refused), write it down, then pay.
    Used by the automatic top-up and by the page's "Order domains" button (a person's click)."""
    s = store.settings()
    create, pay = create or create_bulk, pay or charge
    orders = store.kv_get("bench_orders") or []
    tried = store.kv_get("bench_names_tried") or []
    for _ in range(3):                                   # a name can turn out to be taken: drop it, try the next
        try:
            data = create(_body(s, names))
            break
        except RuntimeError as e:
            tried += names
            store.kv_set("bench_names_tried", tried)
            _log(run, live, "order_failed", False, {"names": names, "error": str(e)[:200]})
            names = candidates(n, taken())
    else:
        raise RuntimeError("could not place the bench order after 3 tries")
    now = store.iso()
    orders += [{"domain": o["domain"].lower(), "record_id": o["id"], "at": now, "paid": False} for o in data["orders"]]
    store.kv_set("bench_orders", orders)                 # written down BEFORE paying
    _log(run, live, "order_created", True, {"domains": [o["domain"] for o in data["orders"]]})
    ok, code = pay(data["recordIds"])
    if ok:
        for o in orders:
            o["paid"] = True
        store.kv_set("bench_orders", orders)
    _log(run, live, "order_charge", ok, {"domains": [o["domain"] for o in data["orders"]], "http": code})
    return {"ordered": [o["domain"] for o in data["orders"]], "paid": ok}


def preview(count):
    """What the Order button would do right now (nothing is bought)."""
    s = store.settings()
    orders = store.kv_get("bench_orders") or []
    today = store.iso()[:10]
    room = max(0, s["bench_order_max_per_day"] - sum(1 for o in orders if o["at"][:10] == today))
    unpaid = [o["domain"] for o in orders if not o.get("paid")]
    count = max(1, min(int(count), room or 1))
    return {"names": candidates(count, taken()), "room_today": room, "unpaid": unpaid, "missing": missing_setup(s),
            "plan": "Microsoft, " + str(s["bench_inbox_quantity"] + 2) + " mailboxes each", "forwarding": s["bench_forwarding_domain"],
            "senders": [f"{u['first_name']} {u['last_name']}" for u in _users(s)]}


def order_now(names):
    """A person pressed Order and confirmed the list. Same safety as the automatic path (daily cap, setup check,
    unpaid orders are retried first), but it does not need rotation_mode=live or bench_autobuy: the click is the approval."""
    s = store.settings()
    gaps = missing_setup(s)
    if gaps:
        raise ValueError("missing: " + "; ".join(gaps))
    orders = store.kv_get("bench_orders") or []
    if any(not o.get("paid") for o in orders):
        return top_up_unpaid("manual")
    today = store.iso()[:10]
    room = s["bench_order_max_per_day"] - sum(1 for o in orders if o["at"][:10] == today)
    names = [n.lower() for n in names][:max(0, room)]
    allowed = set(candidates(200, taken()))
    names = [n for n in names if n in allowed]
    if not names:
        raise ValueError("nothing to order: daily limit reached or the names are no longer available")
    return place("manual:" + store.iso(), True, names, len(names))


def top_up_unpaid(run):
    orders = store.kv_get("bench_orders") or []
    unpaid = [o for o in orders if not o.get("paid")]
    ok, code = charge([o["record_id"] for o in unpaid])
    if ok:
        for o in orders:
            o["paid"] = True
        store.kv_set("bench_orders", orders)
    _log(run, True, "order_charge", ok, {"domains": [o["domain"] for o in unpaid], "http": code})
    return {"retried": [o["domain"] for o in unpaid], "paid": ok}
