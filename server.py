"""CFB Matchup Intelligence: zero-dependency Python web server."""
import json, os, time, threading, urllib.request, urllib.parse, urllib.error
from collections import defaultdict
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT=Path(__file__).resolve().parent
YEAR=int(os.environ.get('CFB_SEASON','2026'))
TTL= int(os.environ.get('CFB_REFRESH_HOURS','24'))*3600
API='https://api.collegefootballdata.com'
CACHE=ROOT/'.cache'
CACHE.mkdir(exist_ok=True)
LOCK=threading.Lock()

def fetch(endpoint, **params):
    key=os.environ.get('CFBD_API_KEY','').strip()
    if not key: raise RuntimeError('Missing CFBD_API_KEY. Set it in Render Environment (not GitHub).')
    query=urllib.parse.urlencode(params)
    url=API+endpoint+('?' + query if query else '')
    req=urllib.request.Request(url,headers={'Authorization':'Bearer '+key,'User-Agent':'CFB-Matchup-Intelligence/1.0','Accept':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=40) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403): raise RuntimeError('CollegeFootballData authorization failed. Check CFBD_API_KEY and API access.') from None
        if exc.code == 429: raise RuntimeError('CollegeFootballData request limit reached. Please try later.') from None
        raise RuntimeError('CollegeFootballData HTTP error '+str(exc.code)) from None

def cached(key,endpoint,expiry=TTL,**params):
    f=CACHE/(key+'.json')
    if f.exists():
        try:
            data=json.loads(f.read_text())
            if time.time()-data['at'] < expiry:return data['payload']
        except Exception:pass
    try:
        payload=fetch(endpoint,**params)
        temp=f.with_suffix('.tmp')
        temp.write_text(json.dumps({'at':time.time(),'payload':payload}))
        temp.replace(f)
        return payload
    except Exception:
        if f.exists():
            try: return json.loads(f.read_text())['payload']
            except Exception:pass
        raise

def num(value):
    if value is None:return None
    try:return float(str(value).replace(',','').split('-')[0])
    except (ValueError,TypeError):return None

def getstat(obj,aliases):
    for k in aliases:
        if k in obj and num(obj[k]) is not None:return num(obj[k])
    return None

def statdict(items):
    return {str(x.get('category','')).replace(' ','').lower():x.get('stat') for x in (items or [])}

METRICS={
 'pass_yds': ['netpassingyards','passingyards','passyds'],
 'rush_yds': ['rushingyards','rushyards'],
 'pass_tds': ['passingtds','passingtouchdowns','passtds'],
 'rush_tds': ['rushingtds','rushingtouchdowns','rushtds'],
 'total_yds': ['totalyards','totaloffense']
}

def metrics(raw):
    return {k:getstat(raw,keys) for k,keys in METRICS.items()}

def average(rows,field):
    vals=[r[field] for r in rows if r.get(field) is not None]
    return round(sum(vals)/len(vals),2) if vals else None

def compute():
    games=cached('games_'+str(YEAR),'/games',year=YEAR,seasonType='regular',classification='fbs')
    teams=cached('teams_'+str(YEAR),'/teams/fbs',year=YEAR)
    now=datetime.now(timezone.utc)
    finished=[g for g in games if g.get('completed') and g.get('homePoints') is not None and g.get('awayPoints') is not None]
    weeks=sorted({int(g['week']) for g in finished if g.get('week') is not None and g.get('week',0)>0})
    weekly=[]
    for w in weeks:
        weekly += cached(f'box_{YEAR}_{w}', '/games/teams', year=YEAR,week=w,seasonType='regular',classification='fbs',expiry=TTL if w==max(weeks) else 60*60*24*365)
    mapping={g.get('id'):g for g in games}
    byteam=defaultdict(list)
    for box in weekly:
        g=mapping.get(box.get('id'))
        if not g or not g.get('completed'):continue
        sides=box.get('teams',[])
        if len(sides)!=2:continue
        for i,t in enumerate(sides):
            opp=sides[1-i]
            parsed=metrics(statdict(t.get('stats')))
            oppparsed=metrics(statdict(opp.get('stats')))
            name=t.get('team')
            if not name:continue
            line={'game_id':box.get('id'),'week':g.get('week'),'date':g.get('startDate'),'opponent':opp.get('team'),'points':num(t.get('points')),'points_allowed':num(opp.get('points'))}
            line.update(parsed)
            for k,v in oppparsed.items():line[k+'_allowed']=v
            byteam[name].append(line)
    result={}
    for t in teams:
        name=t.get('school') or t.get('name')
        if not name:continue
        rows=sorted(byteam[name],key=lambda x:(x['date'] or '',x['game_id'] or 0))
        fields=['points','points_allowed','pass_yds','rush_yds','total_yds','pass_tds','rush_tds','pass_yds_allowed','rush_yds_allowed','total_yds_allowed','pass_tds_allowed','rush_tds_allowed']
        result[name]={'name':name,'conference':t.get('conference') or 'Independent','games':len(rows),'season':{x:average(rows,x) for x in fields},'last5':{x:average(rows[-5:],x) for x in fields},'last3':{x:average(rows[-3:],x) for x in fields},'logs':rows}
    future=[]
    for g in games:
        if g.get('completed') or not g.get('startDate'):continue
        # Calendar should not show months of games ahead
        try:
            start=datetime.fromisoformat(g['startDate'].replace('Z','+00:00'))
            if start < now or (start-now).days > 13:continue
        except ValueError:continue
        future.append({'id':g.get('id'),'week':g.get('week'),'start':g.get('startDate'),'away':g.get('awayTeam'),'home':g.get('homeTeam')})
    return {'year':YEAR,'updated':datetime.now(timezone.utc).isoformat(),'teams':list(result.values()),'games':sorted(future,key=lambda x:x['start']),'note':'Passing TD and rushing TD fields can be null where provider box-score categories are unavailable. None means unavailable, not zero.'}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kw): super().__init__(*args,directory=str(ROOT),**kw)
    def do_GET(self):
        path=self.path.split('?')[0]
        if path not in ('/api/dashboard','/api/players','/api/player-games'): return super().do_GET()
        try:
            if path=='/api/dashboard':
                with LOCK:data=compute()
            elif path=='/api/player-games':
                query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                team=(query.get('team') or [''])[0].strip()
                player=(query.get('player') or [''])[0].strip()
                if not team or not player or len(team)>100 or len(player)>120: raise ValueError('Select a valid team and player')
                all_games=cached('games_'+str(YEAR),'/games',year=YEAR,seasonType='regular',classification='fbs')
                games=sorted((g for g in all_games if g.get('completed') and team in (g.get('homeTeam'),g.get('awayTeam'))),key=lambda g:g.get('startDate') or '')[-5:]
                boxed=cached('playerbox_'+str(YEAR)+'_'+urllib.parse.quote(team,safe='').replace('%','_'),'/games/players',year=YEAR,team=team,seasonType='regular',expiry=TTL)
                byid={b.get('id'):b for b in boxed}
                rows=[]
                for g in games:
                    found={}
                    box=byid.get(g.get('id')) or {}
                    for side in box.get('teams',[]):
                        if side.get('team')!=team: continue
                        for cat in side.get('categories',[]):
                            for typ in cat.get('types',[]):
                                for ath in typ.get('athletes',[]):
                                    if str(ath.get('name','')).casefold()==player.casefold():
                                        found.setdefault(str(cat.get('name','')).lower(),{})[str(typ.get('name','')).upper()]=ath.get('stat')
                    rows.append({'week':g.get('week'),'date':g.get('startDate'),'opponent':g.get('awayTeam') if g.get('homeTeam')==team else g.get('homeTeam'),'stats':found,'reported':bool(found)})
                data={'year':YEAR,'team':team,'player':player,'games':rows,'note':'Unreported box scores are marked unavailable, not zero. Last five completed team games.'}
            else:
                # Season aggregate endpoint; these totals are NOT game logs or player hit rates.
                data={'year':YEAR,'players':cached('players_'+str(YEAR),'/stats/player/season',year=YEAR,seasonType='regular'),'source':'CollegeFootballData season player statistics; per-game detail unavailable in this feed'}
            self.send_json(200,data)
        except ValueError as exc:self.send_json(400,{'error':str(exc)})
        except Exception as exc:self.send_json(503,{'error':str(exc)})
    def send_json(self,status,payload):
        b=json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.send_header('Content-Length',str(len(b)))
        self.end_headers();self.wfile.write(b)

if __name__=='__main__':
    port=int(os.environ.get('PORT','8765'))
    print('CFB dashboard listening',port,flush=True)
    ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
