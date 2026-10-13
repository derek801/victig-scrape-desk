"""Build data/counties.csv: every US county (+DC) with 2024 population and LinkedIn geo id.

Geo ids come from LinkedIn's public jobs typeahead (no login). Ids the team already used in
Sales Nav searches (data/raw/harvested_geo.json, pulled from PhantomBuster run logs) are used
first and also serve as a check on the typeahead's accuracy.
"""
import csv, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


CACHE = RAW / "typeahead_cache.json"
_cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}


NO_LOOKUP = "--no-lookup" in sys.argv   # harvested ids only; the app fills the rest itself, slowly


def typeahead(q):
    if q in _cache:
        return _cache[q]
    if NO_LOOKUP:
        return ""
    url = "https://www.linkedin.com/jobs-guest/api/typeaheadHits?" + urllib.parse.urlencode(
        {"typeaheadType": "GEO", "query": q})
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            hits = json.load(urllib.request.urlopen(req, timeout=30))
            _cache[q] = hits[0]["id"] if hits else None
            if len(_cache) % 25 == 0:
                CACHE.write_text(json.dumps(_cache))
            time.sleep(1.2)
            return _cache[q]
        except Exception as e:  # LinkedIn answers 429 when hurried: back off hard
            print("  retry", q, str(e)[:60], flush=True)
            time.sleep(60 * (attempt + 1))
    return "ERR"


def linkedin_name(county, state):
    return f"{county}, {state}, United States"


def main():
    harvested = json.loads((RAW / "harvested_geo.json").read_text())
    by_key, names = {}, {}
    for name, gid in harvested.items():
        parts = [p.strip() for p in name.split(",")]
        if len(parts) >= 3:
            by_key[(parts[0].lower(), parts[1].lower())] = gid
            names[(parts[0].lower(), parts[1].lower())] = name
    rows = []
    with open(RAW / "co-est2024-alldata.csv", encoding="latin-1") as f:
        for r in csv.DictReader(f):
            if r["SUMLEV"] != "050":
                continue
            rows.append({"fips": r["STATE"] + r["COUNTY"], "state": r["STNAME"], "county": r["CTYNAME"],
                         "population": int(r["POPESTIMATE2024"])})
    print(len(rows), "counties", flush=True)

    # Look up every county the team never searched, plus every 25th harvested one as an accuracy check.
    ids = []
    for i, r in enumerate(rows):
        key = (r["county"].lower(), r["state"].lower())
        if key in by_key and i % 25:
            ids.append(by_key[key])
        else:
            ids.append(typeahead(f'{r["county"]}, {r["state"]}'))
        if i % 200 == 0:
            print(i, "/", len(rows), flush=True)
    CACHE.write_text(json.dumps(_cache))
    agree = disagree = 0
    for r, gid in zip(rows, ids):
        key = (r["county"].lower(), r["state"].lower())
        h = by_key.get(key)
        if h:
            if not gid:
                pass
            elif gid == h:
                agree += 1
            else:
                disagree += 1
                print("MISMATCH", r["county"], r["state"], "typeahead", gid, "harvested", h)
        r["geo_id"] = h or gid
        r["geo_source"] = "harvested" if h else "typeahead"
        r["linkedin_text"] = names.get(key) or linkedin_name(r["county"], r["state"])
    print("typeahead vs harvested: agree", agree, "disagree", disagree)
    with open(ROOT / "data" / "counties.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fips", "state", "county", "population", "geo_id", "geo_source", "linkedin_text"])
        w.writeheader()
        w.writerows(rows)
    print("missing:", sum(1 for r in rows if not r["geo_id"] or r["geo_id"] == "ERR"))


if __name__ == "__main__":
    main()
