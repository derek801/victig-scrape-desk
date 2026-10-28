"""Turn a region into an ordered queue of Sales Nav slices, and reshape the queue as results come in.

A slice = some counties x some headcount bands, sized so its search should return fewer than 2,500
people (Sales Nav shows no more than that for one search). Size is estimated as
    population x sum(band density) x region factor
with band densities fitted from the team's own run history (scripts/calibrate.py) and the region factor
nudged toward reality after every run. A slice that still overflows is split in two and re-queued.
"""
import json

from . import store
from .searchurl import BAND_ORDER

SN_CAP = 2500
RECENT_DAYS = 45


def densities():
    """Results per resident for each headcount band (title filter included)."""
    d = store.kv_get("band_density")
    if d:
        return d
    # Fallback before calibration: ~11 HR-titled profiles per 1,000 residents across bands D-I.
    share = {"B": .03, "C": .12, "D": .22, "E": .15, "F": .10, "G": .17, "H": .08, "I": .13}
    return {b: 0.011 * share[b] / 0.85 for b in BAND_ORDER}


def estimate(pop, bands, factor=1.0):
    d = densities()
    return int(pop * sum(d[b] for b in bands) * factor)


def ratios():
    """What past runs showed per county and band: {fips: {band: real size / national-density estimate}}."""
    out = {}
    for r in store.q("SELECT fips,band,ratio FROM county_ratio"):
        out.setdefault(r["fips"], {})[r["band"]] = r["ratio"]
    return out


def county_est(fips, pop, bands, factor, rat):
    """One county's expected size. Uses what we measured there when we have it (bands never measured there
    borrow that county's average), else the region factor."""
    d, r = densities(), rat.get(fips, {})
    fallback = sum(r.values()) / len(r) if r else factor
    return int(sum(pop * d[b] * r.get(b, fallback) for b in bands))


def _band_groups(est_fn, bands, target):
    """Split a big county's bands into consecutive groups that each fit the target (est_fn(bands) -> size)."""
    groups, cur = [], []
    for b in bands:
        if cur and est_fn(cur + [b]) > target:
            groups.append(cur)
            cur = []
        cur.append(b)
    if cur:
        groups.append(cur)
    return groups


def build_cycle(region_id, cycle):
    """(Re)build the pending queue for one region cycle. Biggest counties first inside each state,
    states in the region's listed order."""
    s = store.settings()
    target, max_c, bands = s["target_results"], s["max_counties_per_slice"], s["bands"]
    reg = store.q1("SELECT * FROM regions WHERE id=?", (region_id,))
    factor = reg["factor"] or 1.0
    rat = ratios()
    order = json.loads(reg["states"])
    rows = store.q("SELECT fips,state,population,last_scraped FROM counties WHERE region_id=? AND geo_id NOT IN "
                   "('', 'ERR', 'NONE')", (region_id,))
    # Counties scraped in the last RECENT_DAYS (by the desk or by hand) go to the back of the queue.
    import datetime as dt
    recent = (store.now() - dt.timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    rows.sort(key=lambda r: ((r["last_scraped"] or "") > recent,
                             order.index(r["state"]) if r["state"] in order else 99, -r["population"]))
    # (county, band) pairs this cycle already scraped or is scraping are never queued again
    covered = {}
    for sl in store.q("SELECT counties,bands FROM slices WHERE region_id=? AND cycle=? AND status IN "
                      "('done','capped','running')", (region_id, cycle)):
        for f in json.loads(sl["counties"]):
            covered.setdefault(f, set()).update(json.loads(sl["bands"]))
    out, cur, cur_est, cur_state = [], [], 0, None

    def flush():
        nonlocal cur, cur_est
        if cur:
            out.append((cur, bands, cur_est))
        cur, cur_est = [], 0

    for r in rows:
        if ((r["last_scraped"] or "") > recent) != cur_state:   # slices may span state borders; recent ones stay apart
            flush()
            cur_state = (r["last_scraped"] or "") > recent
        left = [b for b in bands if b not in covered.get(r["fips"], ())]
        if not left:
            continue
        est_fn = lambda g, r=r: county_est(r["fips"], r["population"], g, factor, rat)  # noqa: E731
        if len(left) < len(bands):               # partly scraped county: finish its remaining bands on its own
            for g in _band_groups(est_fn, left, target):
                out.append(([r["fips"]], g, est_fn(g)))
            continue
        e_all = est_fn(bands)
        if e_all > target:                       # county alone is too big: one slice per band group
            for g in _band_groups(est_fn, bands, target):
                out.append(([r["fips"]], g, est_fn(g)))
            continue
        if cur and (cur_est + e_all > target or len(cur) >= max_c):
            flush()
        cur.append(r["fips"])
        cur_est += e_all
    flush()
    store.x("DELETE FROM slices WHERE region_id=? AND cycle=? AND status='pending'", (region_id, cycle))
    base = (store.q1("SELECT MAX(seq) m FROM slices WHERE region_id=? AND cycle=?", (region_id, cycle))["m"] or 0)
    store.xmany("INSERT INTO slices(region_id,cycle,seq,counties,bands,est,status) VALUES(?,?,?,?,?,?, 'pending')",
                [(region_id, cycle, base + i + 1, json.dumps(c), json.dumps(b), e) for i, (c, b, e) in enumerate(out)])
    return len(out)


def next_slice(region_id):
    reg = store.q1("SELECT cycle FROM regions WHERE id=?", (region_id,))
    return store.q1("SELECT * FROM slices WHERE region_id=? AND cycle=? AND status='pending' ORDER BY seq LIMIT 1",
                    (region_id, reg["cycle"]))


def counties_for(slice_row):
    fips = json.loads(slice_row["counties"])
    rows = {r["fips"]: r for r in store.q(
        f"SELECT fips,geo_id,linkedin_text,population FROM counties WHERE fips IN ({','.join('?' * len(fips))})", fips)}
    return [rows[f] for f in fips if f in rows]


def _split_counties(fips, pops):
    """Two county groups of similar population (biggest first onto the lighter side)."""
    a, b, wa, wb = [], [], 0, 0
    for f in sorted(fips, key=lambda f: -pops.get(f, 0)):
        if wa <= wb:
            a.append(f)
            wa += pops.get(f, 0)
        else:
            b.append(f)
            wb += pops.get(f, 0)
    return [a, b]


def _split_bands(bands):
    """Two consecutive band groups with the most even expected result counts."""
    d = densities()
    tot = sum(d[x] for x in bands)
    best = min(range(1, len(bands)), key=lambda i: abs(2 * sum(d[x] for x in bands[:i]) - tot))
    return [bands[:best], bands[best:]]


def record_result(slice_row, total, got):
    """Close a slice after a successful run. Overflow -> split into two children placed next in line
    (fewer counties first; a single county is split by headcount band)."""
    sid, rid = slice_row["id"], slice_row["region_id"]
    fips, bands = json.loads(slice_row["counties"]), json.loads(slice_row["bands"])
    pop = sum(c["population"] for c in counties_for(slice_row))
    _learn(rid, pop, bands, total)
    _learn_counties(counties_for(slice_row), bands, total)
    store.x("UPDATE counties SET last_scraped=? WHERE fips IN (%s)" % ",".join("?" * len(fips)), [store.iso()] + fips)
    if total is None or total <= SN_CAP:
        store.x("UPDATE slices SET status='done',total=?,got=?,finished_at=? WHERE id=?", (total, got, store.iso(), sid))
        return "done"
    halves = _halves(slice_row)
    if not halves:
        store.x("UPDATE slices SET status='capped',total=?,got=?,finished_at=?,note=? WHERE id=?",
                (total, got, store.iso(), f"single county+band has {total} results; Sales Nav shows 2,500", sid))
        return "capped"
    store.x("UPDATE slices SET status='split',total=?,got=?,finished_at=? WHERE id=?", (total, got, store.iso(), sid))
    whole = sum(w for _, _, w in halves)
    _insert_children(slice_row, [(f, b, int(total * w / whole)) for f, b, w in halves])
    return "split"


def _halves(slice_row):
    """Two smaller slices to replace this one, as (counties, bands, weight), or None if it can't shrink.
    Shed counties first; only a single county gets broken up by headcount band."""
    fips, bands = json.loads(slice_row["counties"]), json.loads(slice_row["bands"])
    if len(fips) > 1:
        pops = {c["fips"]: c["population"] for c in counties_for(slice_row)}
        return [(g, bands, sum(pops.get(f, 0) for f in g) or 1) for g in _split_counties(fips, pops)]
    if len(bands) > 1:
        d = densities()
        return [(fips, g, sum(d[b] for b in g)) for g in _split_bands(bands)]
    return None


def _insert_children(slice_row, kids):
    """Queue (counties, bands, est) children right behind the parent."""
    rid = slice_row["region_id"]
    nxt = store.q1("SELECT MIN(seq) m FROM slices WHERE region_id=? AND cycle=? AND status='pending'",
                   (rid, slice_row["cycle"]))["m"]
    lo = slice_row["seq"]
    hi = nxt if nxt is not None else lo + 1
    for i, (f, b, e) in enumerate(kids):
        store.x("INSERT INTO slices(region_id,cycle,seq,counties,bands,est,status,parent_id) VALUES(?,?,?,?,?,?, 'pending', ?)",
                (rid, slice_row["cycle"], lo + (hi - lo) * (i + 1) / 3, json.dumps(f), json.dumps(b), e,
                 slice_row["id"]))


def current_est(slice_row):
    """Size of a queued slice using everything learned so far (its stored est may be weeks old)."""
    reg = store.q1("SELECT factor FROM regions WHERE id=?", (slice_row["region_id"],))
    rat, bands = ratios(), json.loads(slice_row["bands"])
    return sum(county_est(c["fips"], c["population"], bands, reg["factor"] or 1.0, rat)
               for c in counties_for(slice_row))


def next_ready(region_id):
    """Next slice to launch. Re-estimates it first; if it now looks over Sales Nav's cap it is split
    (counties, then bands) before launch rather than scraped blind and capped. A slice that can't shrink
    further goes ahead and returns the first 2,500."""
    for _ in range(20):
        sl = next_slice(region_id)
        if not sl:
            return None
        e = current_est(sl)
        if e != sl["est"]:
            store.x("UPDATE slices SET est=? WHERE id=?", (e, sl["id"]))
            sl["est"] = e
        halves = _halves(sl) if e > SN_CAP else None
        if not halves:
            return sl
        store.x("UPDATE slices SET status='split',finished_at=?,note=? WHERE id=?",
                (store.iso(), f"estimated {e:,} before launch; split to stay under {SN_CAP:,}", sl["id"]))
        d = {c["fips"]: c for c in counties_for(sl)}
        bands = json.loads(sl["bands"])
        reg = store.q1("SELECT factor FROM regions WHERE id=?", (region_id,))
        rat = ratios()
        _insert_children(sl, [(f, b, sum(county_est(x, d[x]["population"], b, reg["factor"] or 1.0, rat)
                                         for x in f if x in d)) for f, b, _ in halves])
        store.log(f"slice {sl['id']} now estimates {e:,}: split before launch")
    return next_slice(region_id)


def _learn(region_id, pop, bands, total):
    """Move the region factor 30% of the way toward what this run actually showed."""
    if not total or not pop:
        return
    est = estimate(pop, bands, 1.0)
    if est <= 0:
        return
    reg = store.q1("SELECT factor FROM regions WHERE id=?", (region_id,))
    f = (reg["factor"] or 1.0) * 0.7 + (total / est) * 0.3
    store.x("UPDATE regions SET factor=? WHERE id=?", (max(0.2, min(5.0, f)), region_id))


def _learn_counties(cs, bands, total):
    """Remember how this slice's real size compared with the national-density guess, per county and band.
    One county: exact. Several: all share the ratio, blended into any earlier figure."""
    if not total or not cs:
        return
    d = densities()
    est = sum(c["population"] * d[b] for c in cs for b in bands)
    if est <= 0:
        return
    ratio = max(0.05, min(20.0, total / est))
    old = ratios()
    for c in cs:
        for b in bands:
            prev = old.get(c["fips"], {}).get(b)
            r = ratio if len(cs) == 1 or prev is None else (prev + ratio) / 2
            store.x("INSERT INTO county_ratio(fips,band,ratio) VALUES(?,?,?) "
                    "ON CONFLICT(fips,band) DO UPDATE SET ratio=excluded.ratio", (c["fips"], b, r))


def maybe_restart(region_id):
    """Region queue empty: start the next cycle once the current one is old enough."""
    reg = store.q1("SELECT * FROM regions WHERE id=?", (region_id,))
    if next_slice(region_id):
        return False
    first = store.q1("SELECT MIN(started_at) t FROM slices WHERE region_id=? AND cycle=?", (region_id, reg["cycle"]))["t"]
    import datetime as dt
    if first:
        age = (store.now() - dt.datetime.fromisoformat(first)).days
        if age < store.settings()["regap_days"]:
            return False
    store.x("UPDATE regions SET cycle=cycle+1 WHERE id=?", (region_id,))
    n = build_cycle(region_id, reg["cycle"] + 1)
    store.log(f"{reg['name']}: finished cycle {reg['cycle']}, starting cycle {reg['cycle'] + 1} ({n} slices)")
    return True
