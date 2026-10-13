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


def _band_groups(pop, bands, target, factor):
    """Split a big county's bands into consecutive groups that each fit the target."""
    groups, cur = [], []
    for b in bands:
        if cur and estimate(pop, cur + [b], factor) > target:
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
    out, cur, cur_pop, cur_state = [], [], 0, None

    def flush():
        nonlocal cur, cur_pop
        if cur:
            out.append((cur, bands, estimate(cur_pop, bands, factor)))
        cur, cur_pop = [], 0

    for r in rows:
        if (r["state"], (r["last_scraped"] or "") > recent) != cur_state:
            flush()
            cur_state = (r["state"], (r["last_scraped"] or "") > recent)
        left = [b for b in bands if b not in covered.get(r["fips"], ())]
        if not left:
            continue
        if len(left) < len(bands):               # partly scraped county: finish its remaining bands on its own
            for g in _band_groups(r["population"], left, target, factor):
                out.append(([r["fips"]], g, estimate(r["population"], g, factor)))
            continue
        e_all = estimate(r["population"], bands, factor)
        if e_all > target:                       # county alone is too big: one slice per band group
            for g in _band_groups(r["population"], bands, target, factor):
                out.append(([r["fips"]], g, estimate(r["population"], g, factor)))
            continue
        if cur and (estimate(cur_pop + r["population"], bands, factor) > target or len(cur) >= max_c):
            flush()
        cur.append(r["fips"])
        cur_pop += r["population"]
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


def record_result(slice_row, total, got):
    """Close a slice after a successful run. Overflow -> split into two children placed next in line."""
    sid, rid = slice_row["id"], slice_row["region_id"]
    fips, bands = json.loads(slice_row["counties"]), json.loads(slice_row["bands"])
    pop = sum(c["population"] for c in counties_for(slice_row))
    _learn(rid, pop, bands, total)
    store.x("UPDATE counties SET last_scraped=? WHERE fips IN (%s)" % ",".join("?" * len(fips)), [store.iso()] + fips)
    if total is None or total <= SN_CAP:
        store.x("UPDATE slices SET status='done',total=?,got=?,finished_at=? WHERE id=?", (total, got, store.iso(), sid))
        return "done"
    if len(bands) > 1:
        halves = [(fips, bands[:len(bands) // 2]), (fips, bands[len(bands) // 2:])]
    elif len(fips) > 1:
        halves = [(fips[:len(fips) // 2], bands), (fips[len(fips) // 2:], bands)]
    else:
        store.x("UPDATE slices SET status='capped',total=?,got=?,finished_at=?,note=? WHERE id=?",
                (total, got, store.iso(), f"single county+band has {total} results; Sales Nav shows 2,500", sid))
        return "capped"
    store.x("UPDATE slices SET status='split',total=?,got=?,finished_at=? WHERE id=?", (total, got, store.iso(), sid))
    nxt = store.q1("SELECT MIN(seq) m FROM slices WHERE region_id=? AND cycle=? AND status='pending'",
                   (rid, slice_row["cycle"]))["m"]
    lo = slice_row["seq"]
    hi = nxt if nxt is not None else lo + 1
    for i, (f, b) in enumerate(halves):
        store.x("INSERT INTO slices(region_id,cycle,seq,counties,bands,est,status,parent_id) VALUES(?,?,?,?,?,?, 'pending', ?)",
                (rid, slice_row["cycle"], lo + (hi - lo) * (i + 1) / 3, json.dumps(f), json.dumps(b),
                 int(total * len(b) / len(bands) * len(f) / len(fips)), sid))
    return "split"


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
