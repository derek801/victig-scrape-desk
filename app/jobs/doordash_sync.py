"""
DoorDash lead sync -> Bison campaign 10 (send.victig.com).

Why this exists: the Clay scrape tables push leads with
  "Bison: Add lead to workspace"  -> POST /api/leads
  "Bison: Add Lead to Campaign"   -> POST /api/campaigns/{id}/leads/attach-leads
The attach step's run condition needs the create step's data.id, but the create
returns 422 {"message":"The email has already been taken."} for any lead already
in the workspace. Those leads are therefore created once and then NEVER attached
to a campaign again. This job closes that gap, and enforces the gates that Clay
does not.

Each run:
  PRUNE   never-contacted leads out of campaign 10 that are Mimecast-tagged, or
          older than STALE_DAYS (verification too old to trust).
  ATTACH  DoorDash-cohort leads created within FRESH_DAYS that are not Mimecast
          and not already in the campaign.

Cohort is identified by the custom variable 'normalized job title', which only the
DoorDash scrape tables set (SHRM uses linkedin_url/employee_count, G2 uses
'intent score'). Leads already in-sequence elsewhere are not force-added.

Usage:  python doordash_sync.py [--apply]   (default is a dry run)
"""
import os, sys, json, time, datetime, urllib.parse, urllib.request, urllib.error

API_KEY   = os.environ.get('BISON_KEY_VICTIG', '')   # set in Render; never commit the key
BASE      = 'https://send.victig.com'
CAMPAIGN  = 10
MIMECAST  = 18
COHORT_VAR= 'normalized job title'
FRESH_DAYS= 14      # attach scan window; also the recency gate (job runs often, so 14d is ample overlap)
STALE_DAYS= 60      # prune never-contacted leads older than this
GUARD_MIN_SENT = 1000   # don't judge bounce rate on a tiny sample
GUARD_MAX_RATE = 0.04   # pause campaign above 4% bounces today
APPLY     = '--apply' in sys.argv

def _req(path, method='GET', payload=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE+path, data=data, method=method,
        headers={'Authorization':'Bearer '+API_KEY,'Accept':'application/json',
                 'Content-Type':'application/json'})
    return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode())

def get(p, tries=3):
    for a in range(tries):
        try: return _req(p)
        except Exception:
            if a==tries-1: raise
            time.sleep(1.5)

def walk(base_path, keep):
    """cursor-paginate; keep(lead) -> id or None"""
    out=[]; cur=None; pages=0
    while True:
        q = base_path + ('&cursor='+urllib.parse.quote(cur) if cur else '')
        try: d = get(q)
        except Exception as e:
            print("  walk aborted:", str(e)[:90], flush=True); break
        rows = d.get('data', [])
        if not rows: break
        pages += 1
        for l in rows:
            k = keep(l)
            if k is not None: out.append(k)
        cur = d.get('meta',{}).get('next_cursor')
        if not cur: break
        if pages % 300 == 0: print("    ...%d pages, %d hits"%(pages,len(out)), flush=True)
    return out

def _send(path, ids, method):
    payload = {"lead_ids":ids} if method=='DELETE' else {"lead_ids":ids,"allow_parallel_sending":False}
    _req(path, method=method, payload=payload, timeout=180)

def chunked(path, ids, method, label):
    """Send in batches. Bison 422s an entire batch if every lead in it is
    ineligible (already in another sequence, bounced, or unsubscribed), so on
    422 we bisect down to singles and keep the good ones."""
    ok=skip=0
    def push(batch, depth=0):
        nonlocal ok, skip
        if not batch: return
        for a in (1,2,3):
            try:
                _send(path, batch, method); ok += len(batch); return
            except urllib.error.HTTPError as e:
                if e.code == 422:
                    if len(batch) == 1: skip += 1; return
                    mid = len(batch)//2
                    push(batch[:mid], depth+1); push(batch[mid:], depth+1); return
                if a==3: skip += len(batch); print("  %s batch failed: %s"%(label,str(e)[:80]), flush=True); return
                time.sleep(2)
            except Exception as e:
                if a==3: skip += len(batch); print("  %s batch error: %s"%(label,str(e)[:80]), flush=True); return
                time.sleep(2)
    for i in range(0, len(ids), 500):
        push(ids[i:i+500]); time.sleep(0.2)
    return ok, skip

today   = datetime.date.today()
fresh   = (today - datetime.timedelta(days=FRESH_DAYS)).isoformat()
stale   = (today - datetime.timedelta(days=STALE_DAYS)).isoformat()
print("=== doordash_sync %s | apply=%s | fresh>=%s | stale<%s" % (today, APPLY, fresh, stale), flush=True)

# ---- PRUNE -------------------------------------------------------------
nc = 'filters[lead_campaign_status]=never_contacted'
mime = walk('/api/campaigns/%d/leads?pagination_type=cursor&per_page=15&%s&filters[tag_ids][]=%d'
            % (CAMPAIGN, nc, MIMECAST), lambda l: l['id'])
old  = walk('/api/campaigns/%d/leads?pagination_type=cursor&per_page=15&%s'
            '&filters[created_at][criteria]=<&filters[created_at][value]=%s' % (CAMPAIGN, nc, stale),
            lambda l: l['id'])
prune = sorted(set(mime) | set(old))
print("PRUNE  mimecast=%d stale=%d union=%d" % (len(mime), len(old), len(prune)), flush=True)

# ---- ATTACH ------------------------------------------------------------
def is_canadian(l):
    # We never send to Canada, any client. .ca inbox or .ca company site both count.
    if (l.get('email') or '').lower().endswith('.ca'): return True
    for c in (l.get('custom_variables') or []):
        if c.get('name') == 'website' and str(c.get('value') or '').lower().rstrip('/').endswith('.ca'):
            return True
    return False

def want(l):
    names = {c.get('name') for c in (l.get('custom_variables') or [])}
    if COHORT_VAR not in names: return None
    if is_canadian(l): return None
    camps = {c.get('campaign_id') for c in (l.get('lead_campaign_data') or [])}
    if CAMPAIGN in camps: return None
    return l['id']
add = walk('/api/leads?pagination_type=cursor&per_page=15'
           '&filters[created_at][criteria]=>=&filters[created_at][value]=%s'
           '&filters[excluded_tag_ids][]=%d' % (fresh, MIMECAST), want)
print("ATTACH candidates=%d" % len(add), flush=True)

if not APPLY:
    print("(dry run - pass --apply to make changes)"); sys.exit(0)

# ---- BOUNCE GUARD --------------------------------------------------------
# Volume was raised 1k -> 10k/day on 2026-09-22 across the June-2026 domains,
# which bounced 5% on their first real send day. Pause before reputation burns.
def bounce_guard():
    d = today.isoformat()
    s = get('/api/campaigns/%d/line-area-chart-stats?start_date=%s&end_date=%s' % (CAMPAIGN, d, d))
    n = {x['label']: sum(v for _, v in x['dates']) for x in s.get('data', [])}
    sent, bounced = n.get('Sent', 0), n.get('Bounced', 0)
    rate = bounced / sent if sent else 0
    print("GUARD  today sent=%d bounced=%d rate=%.2f%%" % (sent, bounced, rate*100), flush=True)
    if sent >= GUARD_MIN_SENT and rate > GUARD_MAX_RATE:
        _req('/api/campaigns/%d/pause' % CAMPAIGN, method='PATCH')
        print("!!! GUARD TRIPPED - campaign %d PAUSED (bounce %.2f%% > %.0f%%)"
              % (CAMPAIGN, rate*100, GUARD_MAX_RATE*100), flush=True)
try: bounce_guard()
except Exception as e: print("  guard check failed:", str(e)[:90], flush=True)

if prune:
    ok,f = chunked('/api/campaigns/%d/leads'%CAMPAIGN, prune, 'DELETE', 'prune')
    print("PRUNED ok=%d skipped=%d"%(ok,f), flush=True)
if add:
    ok,f = chunked('/api/campaigns/%d/leads/attach-leads'%CAMPAIGN, add, 'POST', 'attach')
    print("ATTACHED ok=%d skipped_ineligible=%d"%(ok,f), flush=True)
print("campaign %d total_leads now: %s" % (CAMPAIGN, get('/api/campaigns/%d'%CAMPAIGN).get('data',{}).get('total_leads')))
