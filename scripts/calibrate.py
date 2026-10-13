"""Fit results-per-resident for each headcount band from the team's PhantomBuster history.

model: search total = sum over included bands of (density[band] x population of the searched counties)
Non-negative least squares on every past run whose counties and total are known. Writes
data/calibration.json, which the app loads on first boot. Re-run after data/pb_history.json is refreshed.
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.searchurl import BAND_ORDER, parse  # noqa: E402

pop = {}
for r in csv.DictReader(open(ROOT / "data" / "counties.csv", encoding="utf-8")):
    if r["geo_id"] not in ("", "ERR", "None"):
        pop[r["geo_id"]] = int(r["population"])
hist = json.loads((ROOT / "data" / "pb_history.json").read_text(encoding="utf-8"))

X, y, skipped = [], [], 0
for h in hist:
    if not h.get("url") or not h.get("total"):
        continue
    p = parse(h["url"])
    pops = [pop.get(g) for g, _ in p["regions"]]
    if not pops or None in pops:
        skipped += 1
        continue
    bands = p["bands"] or BAND_ORDER
    X.append([sum(pops) if b in bands else 0 for b in BAND_ORDER])
    y.append(h["total"])
X, y = np.array(X, float), np.array(y, float)
k, _ = nnls(X / 1e6, y)
k = k / 1e6
pred = X @ k
err = np.abs(pred - y) / np.maximum(y, 1)
print(f"runs used {len(y)} (skipped {skipped} with unknown counties)")
print("density per 1M residents:", {b: round(v * 1e6, 1) for b, v in zip(BAND_ORDER, k)})
print(f"median abs error {np.median(err):.0%}, within 2x: {np.mean((pred / np.maximum(y, 1) < 2) & (pred / np.maximum(y, 1) > .5)):.0%}")
# Floor: a band the fit drove to zero still holds people; give it 5% of the mean density.
floor = float(np.mean(k)) * 0.05
dens = {b: max(float(v), floor) for b, v in zip(BAND_ORDER, k)}
(ROOT / "data" / "calibration.json").write_text(json.dumps(
    {"runs": int(len(y)), "density": dens, "median_abs_error": float(np.median(err))}, indent=1))
print("wrote data/calibration.json")
