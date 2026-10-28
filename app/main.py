"""Scrape Desk: web app + scheduler for VICTIG's PhantomBuster Sales Nav scraping."""
import datetime as dt
import json
import secrets
import urllib.parse
from zoneinfo import ZoneInfo

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import alerts, config, runner, seed, slicer, store

app = FastAPI(title="Scrape Desk", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET or secrets.token_hex(32),
                   https_only=bool(config.PUBLIC_URL.startswith("https")), max_age=30 * 24 * 3600)
STATIC = config.ROOT / "app" / "static"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


@app.on_event("startup")
def _startup():
    store.init()
    seed.seed()
    if config.RUN_SCHEDULER:
        runner.start()


# ---- auth ---------------------------------------------------------------------------------------

def auth_mode():
    if config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET:
        return "google"
    if config.DESK_PASSWORD:
        return "password"
    return "none"


def user(request: Request):
    if auth_mode() == "none":
        return "local"
    return (request.session.get("user") or {}).get("email")


def need_user(request: Request):
    u = user(request)
    if not u:
        raise HTTPException(401, "sign in")
    return u


def need_write(request: Request):
    need_user(request)
    if request.headers.get("x-desk") != "1":
        raise HTTPException(403, "missing x-desk header")


def _redirect_uri(request, path):
    base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
    return base.rstrip("/") + path


@app.get("/login")
def login(request: Request):
    if auth_mode() == "password":
        return HTMLResponse(LOGIN_FORM)
    if auth_mode() == "none":
        return RedirectResponse("/")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    q = {"client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": _redirect_uri(request, "/auth/callback"),
         "response_type": "code", "scope": "openid email profile", "state": state, "hd": config.ALLOWED_DOMAIN,
         "prompt": "select_account"}
    return RedirectResponse(GOOGLE_AUTH + "?" + urllib.parse.urlencode(q))


@app.post("/login")
async def login_password(request: Request):
    form = await request.form()
    if auth_mode() == "password" and secrets.compare_digest(str(form.get("password", "")), config.DESK_PASSWORD):
        request.session["user"] = {"email": "team"}
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(LOGIN_FORM.replace("<!--err-->", "<p class=err>Wrong password.</p>"), 401)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if not code or state != request.session.pop("oauth_state", None):
        return RedirectResponse("/login")
    r = requests.post(GOOGLE_TOKEN, data={"code": code, "client_id": config.GOOGLE_CLIENT_ID,
                                          "client_secret": config.GOOGLE_CLIENT_SECRET,
                                          "redirect_uri": _redirect_uri(request, "/auth/callback"),
                                          "grant_type": "authorization_code"}, timeout=20)
    if not r.ok:
        raise HTTPException(400, "Google sign-in failed. Try again.")
    info = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
                        headers={"Authorization": "Bearer " + r.json()["access_token"]}, timeout=20).json()
    email = info.get("email", "")
    if not info.get("email_verified") or not email.endswith("@" + config.ALLOWED_DOMAIN):
        return HTMLResponse(f"<p>{email or 'That account'} can't use the Scrape Desk. Sign in with your "
                            f"@{config.ALLOWED_DOMAIN} Google account.</p><p><a href='/login'>Try again</a></p>", 403)
    request.session["user"] = {"email": email, "name": info.get("name")}
    return RedirectResponse("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


LOGIN_FORM = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Scrape Desk</title><style>body{font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0;background:#f4f6fb}
form{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 12px #0001;display:grid;gap:12px;width:260px}
input,button{font:inherit;padding:9px 11px;border-radius:8px;border:1px solid #ccd}button{background:#1d4ed8;color:#fff;border:0}
.err{color:#b91c1c;margin:0}</style><form method=post action=/login><b>Scrape Desk</b><!--err-->
<input type=password name=password placeholder=Password autofocus><button>Sign in</button></form>"""


# ---- pages --------------------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    hb = store.kv_get("heartbeat")
    return {"ok": True, "heartbeat": hb}


@app.get("/favicon.png")
def favicon():
    return FileResponse(STATIC / "favicon.png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
def index(request: Request):
    if not user(request):
        return RedirectResponse("/login")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ---- state --------------------------------------------------------------------------------------

def _day(iso_ts):
    return dt.datetime.fromisoformat(iso_ts).astimezone(ZoneInfo(config.TZ)).date().isoformat()


@app.get("/api/state")
def state(request: Request):
    need_user(request)
    s = store.settings()
    regions = store.q("SELECT * FROM regions ORDER BY id")
    for r in regions:
        r["states"] = json.loads(r["states"])
        c = store.q("SELECT status, COUNT(*) n, COALESCE(SUM(got),0) got FROM slices WHERE region_id=? AND cycle=? "
                    "GROUP BY status", (r["id"], r["cycle"]))
        r["slices"] = {x["status"]: x["n"] for x in c}
        r["profiles_cycle"] = sum(x["got"] for x in c)
        cov = store.q1("SELECT COUNT(*) n, SUM(population) pop, SUM(CASE WHEN geo_id='' THEN 1 ELSE 0 END) noid "
                       "FROM counties WHERE region_id=?", (r["id"],))
        r["counties"], r["population"], r["counties_without_id"] = cov["n"], cov["pop"], cov["noid"]
        started = store.q1("SELECT MIN(started_at) t FROM slices WHERE region_id=? AND cycle=?", (r["id"], r["cycle"]))
        r["cycle_started"] = started["t"]
    accts = store.q("SELECT * FROM accounts ORDER BY enabled DESC, key")
    for a in accts:
        a["today_runs"], a["today_got"] = runner.account_today(a["key"])
        a["running"] = store.q1("SELECT id, launched_at, slice_id FROM runs WHERE account_key=? AND "
                                "status IN ('launched','running') AND source='desk'", (a["key"],))
        plan = runner.plan_for(a) if a["region_id"] else None
        if plan:
            sl, url, cs = plan
            a["next"] = {"slice_id": sl["id"], "bands": json.loads(sl["bands"]), "est": sl["est"], "url": url,
                         "counties": [c["linkedin_text"].replace(", United States", "") for c in cs]}
        last = store.q1("SELECT launched_at FROM runs WHERE account_key=? AND got>0 ORDER BY launched_at DESC LIMIT 1",
                        (a["key"],))
        a["last_scrape"] = last["launched_at"] if last else None
    since = (store.now() - dt.timedelta(days=35)).isoformat(timespec="seconds")
    daily = {}
    for r in store.q("SELECT account_key, launched_at, got FROM runs WHERE launched_at>=? AND got>0", (since,)):
        d = daily.setdefault(_day(r["launched_at"]), {})
        d[r["account_key"]] = d.get(r["account_key"], 0) + r["got"]
    runs = store.q("SELECT r.*, a.name FROM runs r LEFT JOIN accounts a ON a.key=r.account_key "
                   "ORDER BY launched_at DESC LIMIT 60")
    for r in runs:
        r["errors"] = json.loads(r["errors"] or "[]")
    return {
        "user": user(request), "auth": auth_mode(), "settings": s, "breaker": runner.breaker(),
        "alerts": alerts.open_alerts(), "regions": regions, "accounts": accts, "daily": daily, "runs": runs,
        "events": store.q("SELECT * FROM events ORDER BY id DESC LIMIT 80"),
        "heartbeat": store.kv_get("heartbeat"), "pb_key": bool(config.PB_API_KEY),
        "pb_storage_full": store.kv_get("pb_storage_full"), "calibration": store.kv_get("calibration"),
        "in_window": runner.in_window(), "tz": config.TZ, "now_local": runner.local_now().isoformat(timespec="minutes"),
        "doordash": {"enabled": s["doordash_sync"], "key": bool(config.BISON_KEY_VICTIG),
                     "last": store.kv_get("doordash_last"), "history": (store.kv_get("doordash_history") or [])[-12:]},
    }


@app.get("/api/coverage")
def coverage(request: Request):
    need_user(request)
    now = store.now()
    out = {}
    for c in store.q("SELECT fips, region_id, last_scraped, geo_id FROM counties"):
        days = (now - dt.datetime.fromisoformat(c["last_scraped"])).days if c["last_scraped"] else None
        out[c["fips"]] = [c["region_id"], days, bool(c["geo_id"])]
    return out


@app.get("/api/regions/{rid}/slices")
def region_slices(rid: int, request: Request):
    need_user(request)
    reg = store.q1("SELECT * FROM regions WHERE id=?", (rid,))
    rows = store.q("SELECT * FROM slices WHERE region_id=? AND cycle=? ORDER BY seq", (rid, reg["cycle"]))
    names = {c["fips"]: c["county"] + ", " + c["state"] for c in store.q("SELECT fips,county,state FROM counties WHERE region_id=?", (rid,))}
    for r in rows:
        f = json.loads(r["counties"])
        r["county_names"] = [names.get(x, x) for x in f]
        r["bands"] = json.loads(r["bands"])
    return rows


# ---- actions ------------------------------------------------------------------------------------

EDITABLE = {"dry_run": bool, "paused": bool, "titles": list, "bands": list, "target_results": int,
            "max_counties_per_slice": int, "window_start": str, "window_end": str, "weekdays_only": bool,
            "max_launches_per_day": int, "regap_days": int, "doordash_sync": bool}


@app.post("/api/settings")
def set_settings(request: Request, patch: dict = Body(...)):
    need_write(request)
    clean = {k: EDITABLE[k](v) for k, v in patch.items() if k in EDITABLE}
    if "titles" in clean:
        clean["titles"] = [t.strip() for t in clean["titles"] if str(t).strip()]
        if not clean["titles"]:
            raise HTTPException(400, "at least one title")
    if "bands" in clean and not clean["bands"]:
        raise HTTPException(400, "at least one headcount band")
    store.save_settings(clean)
    store.log(f"{user(request)} changed settings: {', '.join(f'{k}={v}' for k, v in clean.items())}")
    if {"bands", "target_results", "max_counties_per_slice"} & set(clean):
        for r in store.q("SELECT id, cycle FROM regions"):
            slicer.build_cycle(r["id"], r["cycle"])
        store.log("rebuilt pending slices for every region")
    return {"ok": True}


@app.post("/api/accounts/{key}")
def set_account(key: str, request: Request, patch: dict = Body(...)):
    need_write(request)
    a = store.q1("SELECT * FROM accounts WHERE key=?", (key,))
    if not a:
        raise HTTPException(404)
    if "enabled" in patch:
        store.x("UPDATE accounts SET enabled=? WHERE key=?", (1 if patch["enabled"] else 0, key))
    if "region_id" in patch:
        store.x("UPDATE accounts SET region_id=? WHERE key=?", (patch["region_id"] or None, key))
    if "daily_budget" in patch:
        store.x("UPDATE accounts SET daily_budget=? WHERE key=?", (max(100, min(2500, int(patch["daily_budget"]))), key))
    if patch.get("clear_attention"):
        runner.set_ok(a)
    store.log(f"{user(request)} updated {a['name']}: {patch}")
    return {"ok": True}


@app.post("/api/accounts/{key}/launch-now")
def launch_now(key: str, request: Request):
    """Launch this account's next slice right away (outside the window / budget). Still refuses in dry run."""
    need_write(request)
    s = store.settings()
    a = store.q1("SELECT * FROM accounts WHERE key=?", (key,))
    if s["dry_run"]:
        raise HTTPException(400, "Dry run is on. Turn it off to launch.")
    if not config.PB_API_KEY:
        raise HTTPException(400, "PB_API_KEY is not set.")
    if runner.breaker():
        raise HTTPException(400, "Breaker is tripped. Reset it first.")
    if store.q1("SELECT 1 FROM runs WHERE account_key=? AND status IN ('launched','running')", (key,)):
        raise HTTPException(400, "A run is already going for this account.")
    runner.launch_one(a)
    store.log(f"{user(request)} launched {a['name']} by hand")
    return {"ok": True}


@app.post("/api/breaker/reset")
def breaker_reset(request: Request):
    need_write(request)
    runner.reset_breaker()
    return {"ok": True}


@app.post("/api/slices/{sid}/{action}")
def slice_action(sid: int, action: str, request: Request):
    need_write(request)
    sl = store.q1("SELECT * FROM slices WHERE id=?", (sid,))
    if not sl:
        raise HTTPException(404)
    if action == "skip":
        store.x("UPDATE slices SET status='skipped', note=? WHERE id=?", (f"skipped by {user(request)}", sid))
    elif action == "requeue":
        store.x("UPDATE slices SET status='pending', attempts=0 WHERE id=?", (sid,))
    elif action == "first":
        m = store.q1("SELECT MIN(seq) m FROM slices WHERE region_id=? AND cycle=? AND status='pending'",
                     (sl["region_id"], sl["cycle"]))["m"] or 1
        store.x("UPDATE slices SET seq=? WHERE id=?", (m - 1, sid))
    else:
        raise HTTPException(400)
    store.log(f"{user(request)}: slice {sid} {action}")
    return {"ok": True}


@app.post("/api/states/move")
def move_state(request: Request, body: dict = Body(...)):
    """Move a state to another region (end of that region's list); both regions' pending queues rebuild."""
    need_write(request)
    st, to = body["state"], int(body["region_id"])
    regions = store.q("SELECT * FROM regions")
    frm = None
    for r in regions:
        states = json.loads(r["states"])
        if st in states and r["id"] != to:
            frm = r["id"]
            states.remove(st)
            store.x("UPDATE regions SET states=? WHERE id=?", (json.dumps(states), r["id"]))
        if r["id"] == to and st not in states:
            store.x("UPDATE regions SET states=? WHERE id=?", (json.dumps(states + [st]), to))
    store.x("UPDATE counties SET region_id=? WHERE state=?", (to, st))
    for rid in {frm, to} - {None}:
        reg = store.q1("SELECT cycle FROM regions WHERE id=?", (rid,))
        slicer.build_cycle(rid, reg["cycle"])
    store.log(f"{user(request)} moved {st} to region {to}")
    return {"ok": True}


@app.post("/api/regions/{rid}/restart")
def region_restart(rid: int, request: Request):
    """Start this region over from the top now, regardless of the re-scrape gap."""
    need_write(request)
    reg = store.q1("SELECT * FROM regions WHERE id=?", (rid,))
    store.x("UPDATE slices SET status='skipped', note='region restarted' WHERE region_id=? AND cycle=? AND status='pending'",
            (rid, reg["cycle"]))
    store.x("UPDATE regions SET cycle=cycle+1 WHERE id=?", (rid,))
    n = slicer.build_cycle(rid, reg["cycle"] + 1)
    store.log(f"{user(request)} restarted {reg['name']} (cycle {reg['cycle'] + 1}, {n} slices)")
    return {"ok": True}


@app.post("/api/doordash/run")
def doordash_run(request: Request):
    need_write(request)
    if not config.BISON_KEY_VICTIG:
        raise HTTPException(400, "BISON_KEY_VICTIG is not set.")
    from .jobs import doordash
    import threading
    threading.Thread(target=doordash.run, daemon=True).start()
    store.log(f"{user(request)} started the DoorDash sync by hand")
    return {"ok": True}


@app.exception_handler(HTTPException)
def _http_exc(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
