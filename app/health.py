"""Domain health score, 0-100, and the zone it lands in: Healthy / Watch / Danger / Retire.

One weighted score per sending domain, built only from signals we can actually read. Each signal is turned into
0-100 (100 = perfect); the score is the weighted average of the signals that are available, so a signal we cannot
read yet (spam complaints, blacklists) does not count as a pass or a fail, it is left out and `coverage` shows how
much of the total weight was measured.

Reply rate carries the most weight and is judged against the team's floor (`inbox_reply_good_pct`, 1.0%: at or
above it scores 100, a quarter of it scores 0). It is never a hard-fail by itself: a bad list or bad copy lowers
every domain at once, and that should not look like fifty burnt domains (the rotation's mass-retire stop covers it).
Complaints, authentication and blacklists are the things providers themselves enforce, so they are hard-fails:
they send a domain straight to Retire (or cap it at Danger) whatever the weighted score says.

Pure functions only (no database, no network), so the whole thing can be tested with plain dicts."""

DEFAULT_WEIGHTS = {            # proposed weights; editable in settings ("health_weights")
    "reply": 30,               # reply rate vs the fleet's
    "positive": 15,            # interested replies vs the fleet's
    "warmup": 15,              # Bison warm-up score (inbox placement proxy)
    "complaints": 12,          # spam complaint rate (not readable yet)
    "blacklist": 8,            # major blocklists (not readable yet)
    "auth": 8,                 # SPF / DKIM / DMARC / MX
    "volume": 7,               # sending above the daily limits
    "bounce": 5,               # bounce rate (also a hard-fail at the top end)
}
LABELS = {"reply": "Reply rate", "positive": "Positive replies", "warmup": "Warm-up score", "complaints": "Spam complaints",
          "blacklist": "Blacklists", "auth": "SPF/DKIM/DMARC/MX", "volume": "Volume vs limit", "bounce": "Bounce rate"}

ZONES = (("healthy", 75), ("watch", 55), ("danger", 35))    # score at or above -> zone; below 35 = retire
MIN_WINDOW_SENDS = 200          # a 7/14-day window needs this many sends to be trusted, else fall back to lifetime
PAUSE_BOUNCE_PCT = 2.0          # vendors' common pause line (needs MIN_PAUSE_SENDS in the window)
RETIRE_BOUNCE_PCT = 5.0
MIN_PAUSE_SENDS = 300
COMPLAINT_PAUSE_PCT = 0.1       # Google / Yahoo: stay under 0.1%
COMPLAINT_RETIRE_PCT = 0.3      # Google / Yahoo: never reach 0.3%


def lin(x, bad, good):
    """0 at `bad`, 100 at `good`, straight line between (works whichever of the two is larger)."""
    if x is None:
        return None
    if good == bad:
        return 100.0
    return max(0.0, min(100.0, (x - bad) / (good - bad) * 100))


def zone_for(score, hard_fail=False):
    if hard_fail:
        return "retire"
    for name, floor in ZONES:
        if score >= floor:
            return name
    return "retire"


def auth_points(dns):
    """SPF 30, DMARC 30, DKIM 20, MX 20. None if DNS could not be read (not a fail)."""
    if not dns or dns.get("error"):
        return None
    return 30 * bool(dns.get("spf")) + 30 * bool(dns.get("dmarc")) + 20 * bool(dns.get("dkim")) + 20 * bool(dns.get("mx"))


def score_domain(m, fleet, weights=None):
    """m: sent, bounced, replied, interested (all over the same window), basis ('7d'/'14d'/'lifetime'), mailboxes,
    warmup (average warm-up score or None), dns (dict or None), util (sends vs daily limit, or None),
    complaint_pct / blacklisted (None until we can read them).
    fleet: reply_rate, positive_rate (percent, whole fleet)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    sent = m.get("sent") or 0
    sub = {}
    if sent > 0:
        rr = (m.get("replied") or 0) / sent * 100
        if fleet.get("reply_good_pct"):             # the team's floor: 1% or more is good (0 at a quarter of it)
            sub["reply"] = lin(rr, fleet["reply_good_pct"] * 0.25, fleet["reply_good_pct"])
        elif fleet.get("reply_rate"):
            sub["reply"] = lin(rr / fleet["reply_rate"], 0.25, 1.0)
        if fleet.get("positive_rate"):
            sub["positive"] = lin((m.get("interested") or 0) / sent * 100 / fleet["positive_rate"], 0.25, 1.0)
        bounce = (m.get("bounced") or 0) / sent * 100
        sub["bounce"] = lin(bounce, RETIRE_BOUNCE_PCT, 1.0)
    else:
        bounce = None
    if m.get("warmup") is not None:
        sub["warmup"] = lin(m["warmup"], 50, 95)
    ap = auth_points(m.get("dns"))
    if ap is not None:
        sub["auth"] = float(ap)
    if m.get("util") is not None:
        sub["volume"] = lin(m["util"], 1.5, 1.0)
    cp = m.get("complaint_pct")
    if cp is not None:
        sub["complaints"] = lin(cp, COMPLAINT_RETIRE_PCT, 0.05)
    if m.get("blacklisted") is not None:
        sub["blacklist"] = 0.0 if m["blacklisted"] else 100.0
    sub = {k: v for k, v in sub.items() if v is not None and w.get(k)}
    total_w = sum(w[k] for k in sub)
    if not total_w:
        return {"score": None, "zone": "new", "basis": m.get("basis"), "coverage": 0, "drag": None, "factors": {},
                "hard_fail": [], "capped": []}
    score = sum(w[k] * v for k, v in sub.items()) / total_w
    # hard-fails go straight to Retire; "pause" lines cap the zone at Danger
    hard, capped = [], []
    dns = m.get("dns") or {}
    if cp is not None and cp >= COMPLAINT_RETIRE_PCT:
        hard.append(f"spam complaints {cp:g}% (limit {COMPLAINT_RETIRE_PCT:g}%)")
    elif cp is not None and cp >= COMPLAINT_PAUSE_PCT:
        capped.append(f"spam complaints {cp:g}% (stay under {COMPLAINT_PAUSE_PCT:g}%)")
    if m.get("blacklisted"):
        hard.append("listed on a major blacklist")
    if dns and not dns.get("error") and not dns.get("spf") and not dns.get("dmarc"):
        hard.append("no SPF and no DMARC record")
    if bounce is not None and sent >= MIN_PAUSE_SENDS and m.get("basis") != "lifetime":
        if bounce >= RETIRE_BOUNCE_PCT:
            hard.append(f"bounce rate {bounce:.1f}% over {m.get('basis')}")
        elif bounce >= PAUSE_BOUNCE_PCT:
            capped.append(f"bounce rate {bounce:.1f}% over {m.get('basis')}")
    if capped:
        score = min(score, ZONES[1][1] - 0.1)        # just under the Watch floor = top of Danger
    score = round(score, 1)
    lost = {k: w[k] * (100 - v) / total_w for k, v in sub.items()}
    top = max(lost, key=lost.get)
    return {"score": score, "zone": zone_for(score, bool(hard)), "basis": m.get("basis"),
            "coverage": round(total_w / sum(w.values()) * 100),
            "drag": {"key": top, "label": LABELS[top], "points": round(lost[top], 1)} if lost[top] >= 1 else None,
            "factors": {k: {"score": round(v), "weight": w[k]} for k, v in sub.items()},
            "hard_fail": hard, "capped": capped}
