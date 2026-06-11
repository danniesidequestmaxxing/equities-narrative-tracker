"""Web dashboard (read-only): ticker sentiment lookup + hot tickers.

A FastAPI app + a single-page UI, served as a second Railway service reading the
same Postgres. Prod-only (needs fastapi/uvicorn from the ``prod`` extra).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..admin import service
from ..config import get_settings
from ..db import analytics
from ..db.base import build_engine, build_sessionmaker

_settings = get_settings()
app = FastAPI(title="Narrative Tracker — Dashboard")
_engine = build_engine(_settings.database_url)
_sf = build_sessionmaker(_engine)


class SourceIn(BaseModel):
    handle: str
    tier: str = "COLD"


def _check_token(token: str | None) -> None:
    if not _settings.dashboard_token:
        raise HTTPException(403, "Watchlist management disabled — set NT_DASHBOARD_TOKEN on the web service.")
    if token != _settings.dashboard_token:
        raise HTTPException(401, "Invalid admin token.")


def _since(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/hot")
async def api_hot(hours: float = 24, limit: int = 25) -> list[dict]:
    return await analytics.hot_tickers(_sf, since=_since(hours), limit=limit)


@app.get("/api/ticker/{symbol}")
async def api_ticker(symbol: str, hours: float = 24) -> dict:
    return await analytics.ticker_detail(_sf, symbol=symbol.upper().lstrip("$"), since=_since(hours))


@app.get("/api/sources")
async def api_sources() -> dict:
    """Public read: the current watchlist + whether the UI can manage it."""
    sources = [s for s in await service.list_sources(_sf) if s["active"]]
    return {"manageable": bool(_settings.dashboard_token), "sources": sources}


@app.post("/api/sources")
async def api_add_source(body: SourceIn, x_dashboard_token: str | None = Header(default=None)) -> dict:
    _check_token(x_dashboard_token)
    handle = body.handle.strip().lstrip("@").lower()
    if not handle:
        raise HTTPException(400, "handle is required")
    tier = body.tier.upper()
    if tier not in ("HOT", "WARM", "COLD"):
        tier = "COLD"
    await service.add_source(_sf, platform_user_id=handle, handle=handle, tier=tier)
    return {"ok": True, "handle": handle, "tier": tier}


@app.delete("/api/sources/{handle}")
async def api_remove_source(handle: str, x_dashboard_token: str | None = Header(default=None)) -> dict:
    _check_token(x_dashboard_token)
    removed = await service.remove_source(_sf, platform_user_id=handle.strip().lstrip("@").lower())
    return {"ok": removed}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _HTML


_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Narrative Tracker — Dashboard</title>
<style>
:root{--bg:#0b0f14;--panel:#141b24;--panel2:#1b2430;--line:#243040;--text:#e7eef5;--mut:#7d8e9e;
--green:#3fbf6a;--red:#f1655a;--yellow:#f0b232;--accent:#5aa9e6;--hot:#f1655a;--warm:#f0b232;--cold:#67788a;}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,"Apple Color Emoji",sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap;background:var(--panel)}
header h1{font-size:17px;margin:0;font-weight:650}
.tf{display:flex;gap:6px}.tf button{background:var(--panel2);color:var(--mut);border:1px solid var(--line);
border-radius:7px;padding:6px 11px;font-size:12.5px;cursor:pointer}.tf button.on{background:var(--accent);color:#04121f;border-color:var(--accent);font-weight:650}
.search{margin-left:auto;display:flex;gap:8px}
.search input{background:#0b1219;border:1px solid var(--line);border-radius:8px;padding:8px 12px;color:var(--text);font-size:13.5px;width:170px}
.search button{background:var(--accent);color:#04121f;border:none;border-radius:8px;padding:8px 14px;font-weight:650;cursor:pointer}
.meta{color:var(--mut);font-size:12px}
main{display:grid;grid-template-columns:minmax(380px,1fr) minmax(420px,1.1fr);gap:18px;padding:18px 22px;align-items:start}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:13px;overflow:hidden}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:#9fb0c0;margin:0;padding:13px 16px;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td,th{padding:9px 14px;text-align:left;border-bottom:1px solid #1a2330}
th{color:var(--mut);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--panel2)}
.sym{font-weight:700}.mut{color:var(--mut)}
.bar{height:7px;border-radius:4px;background:#22303f;overflow:hidden;width:90px;display:inline-block;vertical-align:middle}
.bar>span{display:block;height:100%}
.badge{font-size:10px;font-weight:700;padding:2px 6px;border-radius:5px;letter-spacing:.3px}
.HOT{background:rgba(241,101,90,.18);color:var(--hot)}.WARM{background:rgba(240,178,50,.18);color:var(--warm)}.COLD{background:rgba(103,120,138,.18);color:var(--cold)}
.take{padding:12px 16px;border-bottom:1px solid #1a2330}
.take .top{display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-wrap:wrap}
.take a{color:var(--accent);text-decoration:none;font-weight:600}.take .txt{color:#cdd8e3;font-size:13.5px;line-height:1.45}
.gauge{padding:16px;display:flex;gap:22px;align-items:center;border-bottom:1px solid var(--line)}
.gauge .big{font-size:30px;font-weight:750}.gauge .lbl{color:var(--mut);font-size:12px}
.empty{padding:26px 16px;color:var(--mut);text-align:center;font-size:13.5px}
.pos{color:var(--green)}.neg{color:var(--red)}.neu{color:var(--yellow)}
.srcadd{display:flex;gap:8px;padding:13px 16px;flex-wrap:wrap;align-items:center}
.srcadd input,.srcadd select{background:#0b1219;border:1px solid var(--line);border-radius:8px;padding:8px 11px;color:var(--text);font-size:13px}
.srcadd input#newh{width:180px}.srcadd input#tok{width:150px;margin-left:auto}
.srcadd button{background:var(--accent);color:#04121f;border:none;border-radius:8px;padding:8px 16px;font-weight:650;cursor:pointer}
.srcnote{padding:2px 16px;color:var(--mut);font-size:12px;min-height:16px}
.chips{display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--panel2);border:1px solid var(--line);border-radius:20px;padding:5px 7px 5px 11px;font-size:13px}
.chip button.x{cursor:pointer;color:var(--mut);border:none;background:#22303f;border-radius:50%;width:18px;height:18px;line-height:1;font-size:12px;padding:0}
.chip button.x:hover{color:var(--red);background:#3a2326}
</style></head><body>
<header>
  <h1>📈 Narrative Tracker</h1>
  <div class="tf" id="tf"></div>
  <span class="meta" id="meta">—</span>
  <div class="search"><input id="q" placeholder="ticker e.g. NVDA" /><button onclick="lookup()">Look up</button></div>
</header>
<main>
  <div class="card"><h2>🔥 Hot tickers</h2><div id="hot"><div class="empty">loading…</div></div></div>
  <div class="card"><h2 id="dtitle">Ticker detail</h2><div id="detail"><div class="empty">Click a ticker or search above.</div></div></div>
  <div class="card" style="grid-column:1/-1"><h2>⚙️ Manage watchlist</h2>
    <div class="srcadd">
      <input id="newh" placeholder="handle e.g. elonmusk" />
      <select id="newt"><option>COLD</option><option>WARM</option><option>HOT</option></select>
      <input id="tok" type="password" placeholder="admin token" />
      <button onclick="addSrc()">Add account</button>
    </div>
    <div class="srcnote" id="srcnote"></div>
    <div id="srclist" class="chips"><div class="empty">loading…</div></div>
  </div>
</main>
<script>
let HOURS=24, CUR=null;
const TFS=[[1,'1h'],[6,'6h'],[24,'24h'],[72,'3d'],[168,'7d']];
const sgn=s=>s>0.15?'pos':s<-0.15?'neg':'neu', emo=s=>s>0.15?'🟢':s<-0.15?'🔴':'🟡';
const stEmo={bullish:'🟢',bearish:'🔴',neutral:'🟡',unclear:'⚪'};
function bar(s){const p=Math.min(100,Math.abs(s)*100);const c=s>0.15?'var(--green)':s<-0.15?'var(--red)':'var(--yellow)';
return `<span class="bar"><span style="width:${p}%;background:${c}"></span></span>`;}
function esc(t){const d=document.createElement('div');d.textContent=t||'';return d.innerHTML;}
function rel(iso){if(!iso)return'';const m=Math.floor((Date.now()-new Date(iso))/60000);return m<60?m+'m':Math.floor(m/60)+'h';}
function setTf(){const e=document.getElementById('tf');e.innerHTML=TFS.map(([h,l])=>`<button class="${h===HOURS?'on':''}" onclick="HOURS=${h};setTf();refresh()">${l}</button>`).join('');}
async function refresh(){
  const r=await fetch(`/api/hot?hours=${HOURS}&limit=25`); const rows=await r.json();
  document.getElementById('meta').textContent=`${rows.reduce((a,b)=>a+b.mentions,0)} mentions · ${rows.length} tickers · last ${TFS.find(t=>t[0]===HOURS)[1]} · updated ${new Date().toLocaleTimeString()}`;
  const h=document.getElementById('hot');
  if(!rows.length){h.innerHTML='<div class="empty">No mentions yet in this window — the worker will fill this as tweets arrive.</div>';return;}
  h.innerHTML=`<table><thead><tr><th>#</th><th>Ticker</th><th>Mentions</th><th>Sentiment</th><th>Top accounts</th></tr></thead><tbody>`+
   rows.map((t,i)=>`<tr onclick="loadTicker('${t.symbol}')"><td class="mut">${i+1}</td>
   <td><span class="sym">$${esc(t.symbol)}</span> <span class="mut">${t.asset_class}</span></td>
   <td>${t.mentions} <span class="mut">/ ${t.accounts}acct</span></td>
   <td>${emo(t.sentiment)} ${bar(t.sentiment)} <span class="${sgn(t.sentiment)}">${t.sentiment>0?'+':''}${t.sentiment}</span></td>
   <td class="mut">${(t.top_accounts||[]).map(a=>'@'+esc(a)).join(', ')}</td></tr>`).join('')+`</tbody></table>`;
}
async function loadTicker(sym){
  CUR=sym; document.getElementById('dtitle').textContent='$'+sym;
  const d=document.getElementById('detail'); d.innerHTML='<div class="empty">loading…</div>';
  const r=await fetch(`/api/ticker/${encodeURIComponent(sym)}?hours=${HOURS}`); const x=await r.json();
  if(!x.takes||!x.takes.length){d.innerHTML=`<div class="empty">No mentions of $${esc(sym)} in this window.</div>`;return;}
  d.innerHTML=`<div class="gauge"><div><div class="big ${sgn(x.sentiment)}">${emo(x.sentiment)} ${x.sentiment>0?'+':''}${x.sentiment}</div><div class="lbl">credibility-weighted sentiment</div></div>
   <div><div class="big">${x.mentions}</div><div class="lbl">mentions · N_eff ${x.n_eff}</div></div></div>`+
   x.takes.map(t=>`<div class="take"><div class="top">
     <span class="badge ${t.tier}">${t.tier}</span>
     <a href="${t.url}" target="_blank">@${esc(t.handle)}</a>
     <span>${stEmo[t.stance]||''} ${esc(t.stance)} <span class="mut">${t.stance_confidence}</span></span>
     <span class="mut" style="margin-left:auto">${rel(t.posted_at)}</span></div>
     <div class="txt">${esc(t.text)}</div></div>`).join('');
}
function lookup(){const v=document.getElementById('q').value.trim().replace(/^\\$/,'').toUpperCase();if(v)loadTicker(v);}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')lookup();});
function tok(){return document.getElementById('tok').value.trim();}
function note(m){document.getElementById('srcnote').textContent=m;}
async function loadSources(){
  const x=await (await fetch('/api/sources')).json();
  note(x.manageable?'Adds go live on the worker within ~2 min.':'Read-only — set NT_DASHBOARD_TOKEN on the web service to manage accounts from here.');
  const l=document.getElementById('srclist');
  if(!x.sources.length){l.innerHTML='<div class="empty">No accounts on the watchlist yet.</div>';return;}
  l.innerHTML=x.sources.map(s=>`<span class="chip"><span class="badge ${s.tier}">${s.tier}</span>@${esc(s.handle)}<button class="x" title="remove" onclick="rmSrc('${esc(s.handle)}')">×</button></span>`).join('');
}
async function addSrc(){
  const h=document.getElementById('newh').value.trim().replace(/^@/,''); const t=document.getElementById('newt').value;
  if(!h){note('Enter a handle first.');return;}
  const r=await fetch('/api/sources',{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':tok()},body:JSON.stringify({handle:h,tier:t})});
  if(r.ok){document.getElementById('newh').value='';note(`Added @${h} (${t}). Live on the worker within ~2 min.`);loadSources();}
  else{const e=await r.json().catch(()=>({}));note('⚠️ '+(e.detail||('HTTP '+r.status)));}
}
async function rmSrc(h){
  const r=await fetch('/api/sources/'+encodeURIComponent(h),{method:'DELETE',headers:{'X-Dashboard-Token':tok()}});
  if(r.ok){note(`Removed @${h} — the worker stops polling it next cycle.`);loadSources();}
  else{const e=await r.json().catch(()=>({}));note('⚠️ '+(e.detail||('HTTP '+r.status)));}
}
const tokEl=document.getElementById('tok'); tokEl.value=localStorage.getItem('nt_tok')||'';
tokEl.addEventListener('input',()=>localStorage.setItem('nt_tok',tokEl.value.trim()));
document.getElementById('newh').addEventListener('keydown',e=>{if(e.key==='Enter')addSrc();});
setTf(); refresh(); loadSources(); setInterval(()=>{refresh(); loadSources(); if(CUR)loadTicker(CUR);}, 60000);
</script></body></html>"""
