"""PhantomBuster API v2 (https://hub.phantombuster.com/reference). API key is per workspace (org)."""
import json
import re
import time

import requests

from . import config

BASE = "https://api.phantombuster.com/api/v2"
_s = requests.Session()


class PBError(Exception):
    pass


def _h():
    if not config.PB_API_KEY:
        raise PBError("PB_API_KEY is not set")
    return {"X-Phantombuster-Key-1": config.PB_API_KEY, "Accept": "application/json"}


def _call(method, path, **kw):
    last = None
    for attempt in range(3):
        try:
            r = _s.request(method, BASE + path, headers=_h(), timeout=60, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                last = PBError(f"{r.status_code} {r.text[:200]}")
                time.sleep(3 * (attempt + 1))
                continue
            if not r.ok:
                raise PBError(f"{r.status_code} {r.text[:300]}")
            return r.json() if r.text else {}
        except requests.RequestException as e:
            last = PBError(str(e))
            time.sleep(3 * (attempt + 1))
    raise last


def agent(agent_id):
    return _call("GET", "/agents/fetch", params={"id": agent_id})


def agent_argument(agent_id):
    a = agent(agent_id)
    arg = a.get("argument") or "{}"
    return json.loads(arg) if isinstance(arg, str) else arg


def launch(agent_id, argument):
    """Launch with a one-off argument; the agent's saved argument is left as it was."""
    r = _call("POST", "/agents/launch", json={"id": agent_id, "argument": argument})
    cid = r.get("containerId")
    if not cid:
        raise PBError(f"launch returned no containerId: {r}")
    return str(cid)


def containers(agent_id, limit=20):
    r = _call("GET", "/containers/fetch-all", params={"agentId": agent_id, "limit": limit})
    return r.get("containers", r if isinstance(r, list) else [])


def container(cid):
    return _call("GET", "/containers/fetch", params={"id": cid})


def output(cid):
    return (_call("GET", "/containers/fetch-output", params={"id": cid}) or {}).get("output") or ""


def me():
    return _call("GET", "/orgs/fetch")


# ---- run-log parsing -------------------------------------------------------------------------

LOGIN_PATTERNS = [
    "No valid credentials", "was disconnected by LinkedIn", "LinkedIn has been disconnected",
    "Disconnected. using", "Session cookie not valid", "can't connect profile", "2FA security detected",
    "Search couldn't be loaded",
]
IGNORE_PATTERNS = ["reached the limit of 100,000 leads"]  # PB lead storage full; CSV still saves


def parse_output(text):
    got = re.findall(r"\((\d+) this launch\)", text)
    total = re.search(r"Total results count: (\d+)", text)
    errs = sorted({e.strip() for e in re.findall(r"\[error\][^\n]{0,200}", text)})
    real = [e for e in errs if not any(p in e for p in IGNORE_PATTERNS)]
    login = [e for e in real if any(p in e for p in LOGIN_PATTERNS)]
    url = re.search(r"https://www\.linkedin\.com/sales/search/people\S+", text)
    return {
        "got": int(got[-1]) if got else 0,
        "total": int(total.group(1)) if total else None,
        "errors": real,
        "login_problem": bool(login),
        "storage_full": any(p in e for e in errs for p in IGNORE_PATTERNS),
        "saved": "Data successfully saved" in text or "CSV saved at" in text,
        "url": url.group(0) if url else None,
    }
