"""SQLite store. One file on the Render disk; one process writes it."""
import json
import sqlite3
import threading
import datetime as dt

from . import config

_local = threading.local()
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS regions (
  id INTEGER PRIMARY KEY, name TEXT, states TEXT, color TEXT, cycle INTEGER DEFAULT 1, factor REAL DEFAULT 1.0);
CREATE TABLE IF NOT EXISTS accounts (
  key TEXT PRIMARY KEY, name TEXT, agent_id TEXT, region_id INTEGER, enabled INTEGER DEFAULT 1,
  daily_budget INTEGER DEFAULT 2000, status TEXT DEFAULT 'ok', status_note TEXT, status_since TEXT,
  fail_streak INTEGER DEFAULT 0, next_launch_at TEXT);
CREATE TABLE IF NOT EXISTS counties (
  fips TEXT PRIMARY KEY, state TEXT, county TEXT, population INTEGER, geo_id TEXT, linkedin_text TEXT,
  region_id INTEGER, last_scraped TEXT);
CREATE TABLE IF NOT EXISTS slices (
  id INTEGER PRIMARY KEY, region_id INTEGER, cycle INTEGER, seq REAL, counties TEXT, bands TEXT,
  titles TEXT, est INTEGER, status TEXT DEFAULT 'pending', parent_id INTEGER, attempts INTEGER DEFAULT 0,
  total INTEGER, got INTEGER, started_at TEXT, finished_at TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS slices_q ON slices(region_id, cycle, status, seq);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, account_key TEXT, slice_id INTEGER, container_id TEXT, url TEXT,
  launched_at TEXT, finished_at TEXT, status TEXT, exit_code INTEGER, total INTEGER, got INTEGER,
  errors TEXT, source TEXT DEFAULT 'desk');
CREATE INDEX IF NOT EXISTS runs_acct ON runs(account_key, launched_at);
CREATE UNIQUE INDEX IF NOT EXISTS runs_container ON runs(container_id);
CREATE TABLE IF NOT EXISTS county_ratio (
  fips TEXT, band TEXT, ratio REAL, PRIMARY KEY (fips, band));
CREATE TABLE IF NOT EXISTS clay_tables (
  id INTEGER PRIMARY KEY, account_key TEXT, name TEXT, url TEXT, baseline_rows INTEGER, baseline_at TEXT,
  row_limit INTEGER DEFAULT 50000, active INTEGER DEFAULT 1, created_at TEXT, archived_at TEXT);
CREATE TABLE IF NOT EXISTS inbox_senders (
  id INTEGER PRIMARY KEY, email TEXT, domain TEXT, status TEXT, warmup INTEGER, daily_limit INTEGER,
  sent INTEGER, bounced INTEGER, replied INTEGER, interested INTEGER, warmup_score REAL);
CREATE INDEX IF NOT EXISTS inbox_dom ON inbox_senders(domain);
CREATE TABLE IF NOT EXISTS inbox_history (
  day TEXT, domain TEXT, sent INTEGER, bounced INTEGER, mailboxes INTEGER, connected INTEGER,
  replied INTEGER, interested INTEGER, score REAL, zone TEXT,
  PRIMARY KEY (day, domain));
CREATE TABLE IF NOT EXISTS rotation_log (
  id INTEGER PRIMARY KEY, ts TEXT, run TEXT, mode TEXT, step TEXT, domain TEXT, ok INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, text TEXT);
"""

DEFAULT_SETTINGS = {
    "dry_run": True,              # nothing launches until someone turns this off
    "paused": False,
    "titles": ["Human Resources", "Human Resource", "HR", "HRIS", "Talent Acquisition", "Recruiter",
               "Recruiting", "Recruitment", "People Operations", "Chief People Officer", "People and Culture",
               "HR Generalist", "HR Business Partner"],
    "bands": ["D", "E", "F", "G", "H", "I"],   # 51+ employees
    "target_results": 2200,       # aim each slice under Sales Nav's 2,500-result ceiling (buffer for estimate error)
    "max_counties_per_slice": 60,
    "window_start": "09:00",      # local launch window (APP_TZ)
    "window_end": "15:30",
    "weekdays_only": True,
    "max_launches_per_day": 3,
    "regap_days": 30,             # a region restarts from the top only if its last cycle began this long ago
    "inbox_min_sends": 100,       # a domain is judged on lifetime bounce rate once it has this many sends
    "inbox_warn_pct": 2.0,        # Bison: 2-3% is the warning zone ...
    "inbox_crit_pct": 3.0,        # ... 3%+ is critical (replace)
    "inbox_reply_min_sends": 500, # reply rate is judged once a domain has this many sends (replies are rare)
    "inbox_reply_warn_pct": 1.0,  # reply rate: lower is worse. Under 1.0% is the warning zone ...
    "inbox_window_min_sends": 200, # a 7/14-day health window needs this many sends, else the lifetime numbers are used
    "rotation_mode": "dry",       # off | dry (plan and log only) | live (really swaps in Bison). Stays dry until a person flips it
    "rotation_daily_cap": 1,      # at most this many live swaps per day
    "rotation_mass_limit": 5,     # more than this many domains in Retire at once = stop and tell the team (bad list, not burnt domains)
    "bench_target": 5,            # warmed, DNS-verified spare domains to keep ready
    "bench_autobuy": False,       # the desk may order bench domains in Hypertide (also needs rotation_mode=live). OFF until a person turns it on
    "bench_order_max_per_day": 5, # never order more than this many domains in a day (only ever tops the bench up to bench_target)
    "bench_forwarding_domain": "www.victig.com",
    "bench_client_name": "VICTIG",
    "bench_sender_names": ["Josh Diederich", "Morgan Verhoef", "Stephen Treanor"],  # every new bench domain's mailboxes are split evenly across these names
    "bench_inbox_quantity": 50,   # Hypertide: 25 or 50 (creates +2 spare mailboxes)
    "bench_min_days": 14,         # a desk-ordered domain is not bench-ready before this many days of warm-up
    "bench_min_score": 70,        # Hypertide recoveryHealth score a bench domain needs (scale unverified; tune after seeing real ones)
    "health_weights": {},          # overrides for app/health.py DEFAULT_WEIGHTS, e.g. {"reply": 35}
    "inbox_reply_good_pct": 1.0,  # the team's goal: 1% reply rate or more is good; the health score's reply signal hits 100 here
    "inbox_reply_crit_pct": 0.6,  # ... under 0.6% is critical. (Fleet median is about 1.3%, overall about 1.05%.)
    "doordash_sync": False,       # campaign-10 hygiene job (moved off Derek's PC when switched on)
    "doordash_sync_every_min": 120,
}


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).isoformat(timespec="seconds")


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c = c
    return c


def init():
    with _lock:
        conn().executescript(SCHEMA)
        try:                                      # databases made before slices could split by title
            conn().execute("ALTER TABLE slices ADD COLUMN titles TEXT")
        except sqlite3.OperationalError:
            pass
        for tbl, col, typ in (("inbox_senders", "interested", "INTEGER"), ("inbox_senders", "warmup_score", "REAL"),
                              ("inbox_history", "replied", "INTEGER"), ("inbox_history", "interested", "INTEGER"),
                              ("inbox_history", "score", "REAL"), ("inbox_history", "zone", "TEXT")):
            try:                                  # databases made before the health score existed
                conn().execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        conn().commit()


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def x(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        conn().commit()
        return cur.lastrowid


def xmany(sql, rows):
    with _lock:
        conn().executemany(sql, rows)
        conn().commit()


def kv_get(k, default=None):
    r = q1("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(r["v"]) if r else default


def kv_set(k, v):
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(kv_get("settings", {}))
    return s


def save_settings(patch):
    cur = kv_get("settings", {})
    cur.update(patch)
    kv_set("settings", cur)


def log(text, level="info"):
    x("INSERT INTO events(ts,level,text) VALUES(?,?,?)", (iso(), level, text))
