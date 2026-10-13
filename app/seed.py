"""First-boot data: regions, accounts, counties, band densities fitted from history, past runs.

Runs automatically when the database is empty (Render's first boot) and can be re-run by hand with
`python -m app.seed --force` (wipes and rebuilds; only do that before go-live)."""
import csv
import json
import sys

from . import config, slicer, store
from .searchurl import BAND_ORDER, parse

# Five regions of roughly equal population (~62-74M each). States are listed in working order.
REGIONS = [
    (1, "Southeast", "#2563eb", ["Florida", "Georgia", "North Carolina", "South Carolina", "Tennessee", "Alabama",
                                 "Mississippi"]),
    (2, "Northeast", "#7c3aed", ["Virginia", "New Jersey", "New York", "Pennsylvania", "Massachusetts", "Maryland",
                                 "Connecticut", "District of Columbia", "Delaware", "Rhode Island", "New Hampshire",
                                 "Maine", "Vermont"]),
    (3, "South Central", "#d97706", ["Oklahoma", "Texas", "Louisiana", "Arkansas", "Missouri", "Kansas", "Colorado",
                                     "New Mexico", "Nebraska", "South Dakota", "North Dakota"]),
    (4, "Midwest", "#059669", ["Minnesota", "Michigan", "Illinois", "Ohio", "Indiana", "Wisconsin", "Iowa",
                               "Kentucky", "West Virginia"]),
    (5, "West", "#dc2626", ["Oregon", "California", "Washington", "Arizona", "Nevada", "Utah", "Idaho", "Montana",
                            "Wyoming", "Alaska", "Hawaii"]),
]

# Region per account follows where each rep is already working (Max: GA, Blake: VA, Derek's agent: OK,
# Dustin: MN, Brent: OR/CO). Morgan's and Stephen's phantoms are idle, so they start disabled.
ACCOUNTS = [
    ("max", "Max Ramey", "6910468073602201", 1, 1),
    ("blake", "Blake Visser", "2535259615255975", 2, 1),
    ("derek", "Derek Wood", "8339609014873777", 3, 1),
    ("dustin", "Dustin Roethel", "438255430937874", 4, 1),
    ("brent", "Brent Ramey", "8861161839652320", 5, 1),
    ("morgan", "Morgan Verhoef", "4582003922436319", None, 0),
    ("stephen", "Stephen Treanor", "8435947861758237", None, 0),
]
AGENT_TO_KEY = {"max": "max", "Blake 2": "blake", "Derek": "derek", "Dustin": "dustin", "Brent": "brent",
                "Morgan": "morgan", "Stephen": "stephen"}


def load_counties():
    region_of = {st: rid for rid, _, _, states in REGIONS for st in states}
    rows = []
    with open(config.SEED_DIR / "counties.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gid = r["geo_id"] if r["geo_id"] not in ("", "ERR", "None", "NONE") else ""
            rows.append((r["fips"], r["state"], r["county"], int(r["population"]), gid, r["linkedin_text"],
                         region_of.get(r["state"])))
    store.xmany("INSERT OR REPLACE INTO counties(fips,state,county,population,geo_id,linkedin_text,region_id) "
                "VALUES(?,?,?,?,?,?,?)", rows)
    return len(rows)


def load_calibration():
    """Band densities fitted offline by scripts/calibrate.py from the team's run history."""
    p = config.SEED_DIR / "calibration.json"
    if not p.exists():
        return None
    c = json.loads(p.read_text())
    store.kv_set("band_density", c["density"])
    store.kv_set("calibration", c)
    return c


def import_history(history):
    rows = []
    for h in history:
        key = AGENT_TO_KEY.get(h["agent"])
        if not key:
            continue
        rows.append((key, None, str(h["containerId"]), h.get("url"),
                     store.iso(_ts(h["created"])), store.iso(_ts(h["ended"])) if h.get("ended") else None,
                     "finished" if h.get("exit") == 0 else "failed", h.get("exit"), h.get("total"), h.get("got"),
                     json.dumps([e for e in h.get("errs", []) if "100,000" not in e]), "history"))
    store.xmany("INSERT OR IGNORE INTO runs(account_key,slice_id,container_id,url,launched_at,finished_at,status,"
                "exit_code,total,got,errors,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def mark_history_coverage(history):
    """Counties the team already scraped by hand get their real last-scraped date (map + queue order)."""
    by_geo = {r["geo_id"]: r["fips"] for r in store.q("SELECT geo_id, fips FROM counties WHERE geo_id!=''")}
    last = {}
    for h in history:
        if not h.get("url") or not h.get("got"):
            continue
        t = store.iso(_ts(h["created"]))
        for g, _ in parse(h["url"])["regions"]:
            f = by_geo.get(g)
            if f and t > last.get(f, ""):
                last[f] = t
    store.xmany("UPDATE counties SET last_scraped=? WHERE fips=?", [(t, f) for f, t in last.items()])


def _ts(ms):
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def seed(force=False):
    store.init()
    if store.q1("SELECT COUNT(*) n FROM regions")["n"] and not force:
        return False
    if force:
        for t in ("regions", "accounts", "counties", "slices", "runs", "events", "kv"):
            store.x(f"DELETE FROM {t}")
    store.xmany("INSERT INTO regions(id,name,color,states) VALUES(?,?,?,?)",
                [(rid, n, c, json.dumps(st)) for rid, n, c, st in REGIONS])
    store.xmany("INSERT INTO accounts(key,name,agent_id,region_id,enabled) VALUES(?,?,?,?,?)", ACCOUNTS)
    n = load_counties()
    hist_path = config.SEED_DIR / "pb_history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    cal = load_calibration()
    used = cal["runs"] if cal else 0
    imported = import_history(history)
    mark_history_coverage(history)
    slices = {rid: slicer.build_cycle(rid, 1) for rid, *_ in REGIONS}
    store.log(f"seeded: {n} counties, {imported} past runs, calibration from {used} runs, slices {slices}")
    return True


if __name__ == "__main__":
    print(seed(force="--force" in sys.argv))
