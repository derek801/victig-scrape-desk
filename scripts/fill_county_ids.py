"""One-time, resumable fill of missing LinkedIn county geo ids in data/counties.csv.

Same public endpoint the app's geo.py uses (no login), but run from a laptop and committed to the repo so the
deployed desk never has to look anything up. Serial and slow on purpose: LinkedIn answers 429 when hurried,
so a 429 pauses for an hour. Safe to stop (Ctrl+C) and restart; progress is saved every 10 lookups.

Counties whose lookup returns more than one hit (independent cities vs counties, etc.) take the first hit but
are listed in data/geo_ambiguous.csv for a human check against Sales Nav.

    python scripts/fill_county_ids.py [--delay 10] [--limit N]
"""
import csv
import json
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "counties.csv"
AMBIG_PATH = ROOT / "data" / "geo_ambiguous.csv"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
URL = "https://www.linkedin.com/jobs-guest/api/typeaheadHits"
FIELDS = ["fips", "state", "county", "population", "geo_id", "geo_source", "linkedin_text"]


def arg(name, default):
    return type(default)(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def lookup(county, state):
    """-> list of ids (best first). Raises on HTTP errors; 429 raises RuntimeError('429')."""
    url = URL + "?" + urllib.parse.urlencode({"typeaheadType": "GEO", "query": f"{county}, {state}"})
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        return [h["id"] for h in json.load(urllib.request.urlopen(req, timeout=30))]
    except urllib.error.HTTPError as e:
        raise RuntimeError("429" if e.code == 429 else f"HTTP {e.code}")


def save(rows):
    tmp = CSV_PATH.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(CSV_PATH)


def main():
    delay, limit = arg("--delay", 10.0), arg("--limit", 0)
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    ambiguous = list(csv.DictReader(open(AMBIG_PATH, encoding="utf-8"))) if AMBIG_PATH.exists() else []
    seen_ambig = {r["fips"] for r in ambiguous}

    # Sanity check first: the lookup must agree with ids the team really used (spread over the biggest ones).
    known = [r for r in rows if r["geo_source"] == "harvested"]
    known.sort(key=lambda r: -int(r["population"]))
    bad = 0
    for r in known[:150:15]:
        while True:
            try:
                got = lookup(r["county"], r["state"])
                break
            except Exception as e:
                print(f"{e} during the accuracy check: pausing 1h", flush=True)
                time.sleep(3600)
        ok = bool(got) and got[0] == r["geo_id"]
        bad += not ok
        print(("ok   " if ok else "DIFF ") + f"{r['county']}, {r['state']}: team {r['geo_id']} lookup {got[:2]}", flush=True)
        time.sleep(delay)
    if bad > 1:
        sys.exit(f"{bad}/10 known ids disagree with the lookup; not filling anything.")

    todo = [r for r in rows if r["geo_id"] in ("", "ERR", "None", "NONE") and r["geo_source"] != "none_found"]
    todo.sort(key=lambda r: -int(r["population"]))
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} counties to look up at ~{delay:.0f}s each (~{len(todo) * delay / 3600:.1f}h)", flush=True)
    n = 0
    for r in todo:
        try:
            ids = lookup(r["county"], r["state"])
        except Exception as e:
            print(f"{e} on {r['county']}, {r['state']}: pausing 1h", flush=True)
            save(rows)
            time.sleep(3600)
            continue
        if ids:
            r["geo_id"], r["geo_source"] = ids[0], "typeahead"
            if len(ids) > 1 and r["fips"] not in seen_ambig:
                ambiguous.append({"fips": r["fips"], "state": r["state"], "county": r["county"],
                                  "chosen": ids[0], "others": " ".join(ids[1:])})
        else:
            r["geo_id"], r["geo_source"] = "NONE", "none_found"
        n += 1
        if n % 10 == 0:
            save(rows)
            with open(AMBIG_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["fips", "state", "county", "chosen", "others"], lineterminator="\n")
                w.writeheader()
                w.writerows(ambiguous)
            print(f"{n}/{len(todo)} done ({len(ambiguous)} ambiguous)", flush=True)
        time.sleep(delay + random.uniform(0, 3))
    save(rows)
    with open(AMBIG_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fips", "state", "county", "chosen", "others"], lineterminator="\n")
        w.writeheader()
        w.writerows(ambiguous)
    print("finished", n, "lookups", flush=True)


if __name__ == "__main__":
    main()
