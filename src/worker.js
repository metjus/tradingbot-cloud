import {fresh,step,act,view,assertSafe} from './engine.js';
import {HTML, CLIENT} from './ui.js';
import {loadMarket,refreshMarket} from './market.js';
import {configStatus,readDemo} from './trading212.js';
import {newRealPaper,previewPaper,simulateRealStep} from './real-paper.js';
const HEADERS={'Cache-Control':'no-store, private','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"};
const json=(obj,status=200)=>new Response(JSON.stringify(obj),{status,headers:{...HEADERS,'Content-Type':'application/json; charset=utf-8'}});
const page=(html,status=200)=>new Response(html,{status,headers:{...HEADERS,'Content-Type':'text/html; charset=utf-8'}});
const hmac=async(secret,text)=>{let k=await crypto.subtle.importKey('raw',new TextEncoder().encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);let sig=await crypto.subtle.sign('HMAC',k,new TextEncoder().encode(text));return Array.from(new Uint8Array(sig),x=>x.toString(16).padStart(2,'0')).join('')};
async function createSession(secret){const expiry=Date.now()+12*3600e3,nonce=crypto.randomUUID();const data=`${expiry}.${nonce}`;return `${data}.${await hmac(secret,data)}`}
async function authenticated(req,secret){
  const token=/(?:^|;\s*)tb_session=([^;]+)/.exec(req.headers.get('Cookie')||'')?.[1];
  if(!token||token.length>200)return false;
  const [ex,nonce,sig]=token.split('.');if(!ex||!nonce||!sig||!/^\d+$/.test(ex)||!/^[0-9a-f]{64}$/.test(sig))return false;
  if(+ex<Date.now()||+ex>Date.now()+13*3600e3)return false;
  const expected=await hmac(secret,`${ex}.${nonce}`);
  // String comparison is not perfectly constant-time, but signature validation requires HMAC knowledge.
  return expected===sig;
}
const COOKIE_OPTIONS='HttpOnly; Secure; SameSite=Strict; Path=/';
function sameOrigin(req){const origin=req.headers.get('Origin');return origin===new URL(req.url).origin}
async function ensureDB(db){await db.prepare('CREATE TABLE IF NOT EXISTS paper_state (id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL DEFAULT 0, json TEXT NOT NULL, updated_at TEXT NOT NULL)').run()}
async function mutate(db,fn){
  await ensureDB(db);
  for(let attempt=0;attempt<8;attempt++){
    let row=await db.prepare('SELECT revision,json FROM paper_state WHERE id=1').first();
    let state=row?JSON.parse(row.json):fresh();assertSafe(state);
    let out=await fn(state);
    if(out?.ok===false)return out;
    const rev=row?.revision??0,encoded=JSON.stringify(state),now=new Date().toISOString();let result;
    if(row){result=await db.prepare('UPDATE paper_state SET revision=?, json=?, updated_at=? WHERE id=1 AND revision=?').bind(rev+1,encoded,now,rev).run()}
    else{result=await db.prepare('INSERT OR IGNORE INTO paper_state(id,revision,json,updated_at) VALUES(1,?,?,?)').bind(1,encoded,now).run()}
    if(result.meta.changes===1)return {ok:true,data:out};
  }
  return {ok:false,message:'Concurrent update: retry operation'};
}
async function getState(db){await ensureDB(db);const row=await db.prepare('SELECT json,updated_at FROM paper_state WHERE id=1').first();return {state:view(row?JSON.parse(row.json):fresh()),updatedAt:row?.updated_at||null}}
// Isolated USD sandbox: never touches the synthetic paper_state row.
async function ensureRealPaper(db){await db.prepare('CREATE TABLE IF NOT EXISTS real_price_paper (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, json TEXT NOT NULL)').run()}
async function getRealPaper(db){
 await ensureRealPaper(db);
 const row=await db.prepare('SELECT revision,json FROM real_price_paper WHERE id=1').first();
 return {revision:row?.revision??0,state:row?JSON.parse(row.json):newRealPaper()};
}
async function checkRealPaper(db,market){
 await ensureRealPaper(db);
 for(let attempt=0;attempt<5;attempt++){
  const {revision,state}=await getRealPaper(db);
  const out=simulateRealStep(state,market);
  if(!out.ok)return {ok:false,reason:out.reason};
  const json=JSON.stringify(state);let result;
  if(revision===0){result=await db.prepare('INSERT OR IGNORE INTO real_price_paper(id,revision,json) VALUES(1,1,?)').bind(json).run()}
  else{result=await db.prepare('UPDATE real_price_paper SET revision=?,json=? WHERE id=1 AND revision=?').bind(revision+1,json,revision).run()}
  if(result.meta.changes===1)return {ok:true,signals:out.signals,marketPaper:previewPaper(state,market)};
 }
 return {ok:false,reason:'CONCURRENT_UPDATE'};
}
const LOGIN_PAGE=`<!doctype html><html lang="sk"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Bot · Login</title><style>body{font:16px system-ui;background:#0b1120;color:#ecf2ff;display:grid;min-height:100vh;place-items:center;margin:0}form{background:#151f32;padding:30px;border:1px solid #2a3650;border-radius:18px;width:min(340px,80vw)}input,button{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:9px}input{background:#0b1120;color:white;border:1px solid #41506a}button{background:#4d77e8;color:white;border:0;font-weight:bold}small{color:#9ba9c4}#err{color:#ff8b97}</style><form id="f"><h2>Trading Bot Cloud</h2><small>Súkromný paper trading · v0.3.4</small><input type="password" name="password" placeholder="Prístupové heslo" autocomplete="current-password" required><button>Prihlásiť</button><p id="err"></p></form><script src="/login.js"></script></html>`;
export default {
  async fetch(req,env){
    try{
      if(!env.DB || !env.DASHBOARD_PASSWORD || env.DASHBOARD_PASSWORD.length<16)return json({message:'Missing D1 binding or DASHBOARD_PASSWORD (minimum 16 characters).'},503);
      const url=new URL(req.url);const path=url.pathname;
      if(path==='/api/login' && req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        const body=await req.json().catch(()=>null);
        if(typeof body?.password!=='string'||body.password.length>500)return json({message:'Invalid input'},400);
        // Compare hash rather than plaintext, with no account enumeration. Cloudflare Access + WAF rate limiting recommended.
        const expected=await hmac(env.DASHBOARD_PASSWORD,'tradingbot-check');
        const actual=await hmac(body.password,'tradingbot-check');
        if(expected!==actual)return json({message:'Invalid credentials'},401);
        const t=await createSession(env.DASHBOARD_PASSWORD);const r=json({ok:true});r.headers.append('Set-Cookie',`tb_session=${t}; Max-Age=43200; ${COOKIE_OPTIONS}`);return r;
      }
      if(path==='/health')return json({ok:true,version:'0.3.4-t212-demo-readonly'});
      if(path==='/login.js'&&req.method==='GET')return new Response("document.getElementById('f').onsubmit=async e=>{e.preventDefault();let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:e.target.password.value})});if(r.ok)location.href='/';else document.getElementById('err').textContent='Nesprávne heslo alebo chyba prihlásenia.'}",{headers:{...HEADERS,'Content-Type':'application/javascript; charset=utf-8'}});
      const authed=await authenticated(req,env.DASHBOARD_PASSWORD);
      if(!authed){if(path==='/' && req.method==='GET')return page(LOGIN_PAGE);return json({message:'Not authenticated'},401)}
      if(path==='/api/logout'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        let r=json({ok:true});r.headers.set('Set-Cookie',`tb_session=; Max-Age=0; ${COOKIE_OPTIONS}`);return r;
      }
      if(path==='/'&&req.method==='GET')return page(HTML);
      if(path==='/client.js'&&req.method==='GET')return new Response(CLIENT,{headers:{...HEADERS,'Content-Type':'application/javascript; charset=utf-8'}});
      if(path==='/api/state'&&req.method==='GET'){
        const market=await loadMarket(env.DB,env);
        const {state:realPaper}=await getRealPaper(env.DB);
        return json({...await getState(env.DB),market,broker:configStatus(env),marketPaper:previewPaper(realPaper,market)});
      }
      if(path==='/api/market-paper/check'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        const market=await loadMarket(env.DB,env);
        const result=await checkRealPaper(env.DB,market);
        return json(result,result.ok?200:409);
      }
      // Manual demo check only. No periodic broker polling or trading endpoints.
      if(path==='/api/t212/demo'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        return json(await readDemo(env));
      }
      if(path==='/api/action'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        let body=await req.json().catch(()=>null),action=body?.action;
        if(!['start','pause','stop','reset'].includes(action))return json({message:'Unknown action'},400);
        const result=await mutate(env.DB,s=>act(s,action));return json(result,result.ok?200:409);
      }
      return json({message:'Not found'},404);
    }catch(e){console.error('Worker error',e);return json({message:'Internal error'},500)}
  },
  async scheduled(_event,env,ctx){
    ctx.waitUntil((async()=>{
      if(!env.DB)return;
      // No updates when completely inactive, to avoid unnecessary D1 writes.
      const r=await mutate(env.DB,s=>{if(s.running||s.pause_requested||Object.keys(s.positions).length>0)step(s);return {ok:true}});
      if(!r.ok)console.error('Scheduled tick retry exhausted');
      // Market watch runs only every five minutes during the NY regular session.
      // Its data is observational, never fed into the synthetic execution engine.
      if(env.TWELVEDATA_API_KEY&&new Date().getUTCMinutes()%5===0){
        try{await refreshMarket(env.DB,env)}catch(e){console.error('Market watch refresh failed',String(e?.message||e))}
      }
    })());
  }
};
