"""Fill in LinkedIn geo ids for counties that don't have one yet, a few per minute, from LinkedIn's public
jobs typeahead (no login). LinkedIn answers 429 when hurried, so this backs off for an hour on any 429."""
import datetime as dt

import requests

from . import store

URL = "https://www.linkedin.com/jobs-guest/api/typeaheadHits"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
PER_TICK = 2


def lookup(q):
    r = requests.get(URL, params={"typeaheadType": "GEO", "query": q}, headers={"User-Agent": UA}, timeout=20)
    if r.status_code == 429:
        raise RuntimeError("429")
    r.raise_for_status()
    hits = r.json()
    return hits[0]["id"] if hits else None


def validate_some():
    """Before trusting the typeahead, check it against 12 ids the team used in real searches.
    Returns True once validated; on more than one disagreement, filling stops and alerts."""
    v = store.kv_get("geo_validation") or {"checked": [], "bad": []}
    if len(v["checked"]) >= 12:
        return len(v["bad"]) <= 1
    done = set(v["checked"])
    rows = [r for r in store.q("SELECT fips, county, state, geo_id FROM counties WHERE geo_id NOT IN ('', 'NONE') "
                               "ORDER BY population DESC LIMIT 200") if r["fips"] not in done][::15][:PER_TICK]
    for r in rows:
        try:
            gid = lookup(f"{r['county']}, {r['state']}")
        except Exception:
            store.kv_set("geo_hold_until", store.iso(store.now() + dt.timedelta(hours=1)))
            break
        v["checked"].append(r["fips"])
        if gid != r["geo_id"]:
            v["bad"].append([r["county"], r["state"], r["geo_id"], gid])
    store.kv_set("geo_validation", v)
    if len(v["checked"]) >= 12:
        if len(v["bad"]) > 1:
            from . import alerts
            alerts.raise_alert("geo", f"LinkedIn location lookup disagreed with known ids {len(v['bad'])}/12 times; "
                                      "not filling missing counties automatically.", grace_min=0, remind_h=168)
        else:
            store.log(f"location lookup validated: {12 - len(v['bad'])}/12 match ids the team used")
    return False


def fill_some():
    hold = store.kv_get("geo_hold_until")
    if hold and store.now() < dt.datetime.fromisoformat(hold):
        return 0
    if not validate_some():
        return 0
    rows = store.q("SELECT fips, county, state, region_id FROM counties WHERE geo_id='' ORDER BY population DESC LIMIT ?",
                   (PER_TICK,))
    done, touched = 0, set()
    for r in rows:
        try:
            gid = lookup(f"{r['county']}, {r['state']}")
        except Exception as e:
            store.kv_set("geo_hold_until", store.iso(store.now() + dt.timedelta(hours=1)))
            if "429" not in str(e):
                store.log(f"geo lookup {r['county']}, {r['state']}: {e}", "warn")
            break
        store.x("UPDATE counties SET geo_id=? WHERE fips=?", (gid or "NONE", r["fips"]))
        touched.add(r["region_id"])
        done += 1
    if done and not store.q1("SELECT 1 FROM counties WHERE geo_id='' AND region_id IN (%s)" %
                             ",".join(str(t) for t in touched if t) or "0"):
        # a region just became complete: fold its new counties into its pending queue
        from . import slicer
        for rid in touched:
            if rid:
                reg = store.q1("SELECT cycle FROM regions WHERE id=?", (rid,))
                slicer.build_cycle(rid, reg["cycle"])
                store.log(f"region {rid}: all county ids found, pending slices rebuilt")
    return done
