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
  est INTEGER, status TEXT DEFAULT 'pending', parent_id INTEGER, attempts INTEGER DEFAULT 0,
  total INTEGER, got INTEGER, started_at TEXT, finished_at TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS slices_q ON slices(region_id, cycle, status, seq);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, account_key TEXT, slice_id INTEGER, container_id TEXT, url TEXT,
  launched_at TEXT, finished_at TEXT, status TEXT, exit_code INTEGER, total INTEGER, got INTEGER,
  errors TEXT, source TEXT DEFAULT 'desk');
CREATE INDEX IF NOT EXISTS runs_acct ON runs(account_key, launched_at);
CREATE UNIQUE INDEX IF NOT EXISTS runs_container ON runs(container_id);
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
