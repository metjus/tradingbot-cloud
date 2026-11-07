import {fresh,step,act,view,assertSafe} from './engine.js';
import {HTML, CLIENT} from './ui.js';
import {loadMarket,refreshMarket} from './market.js';
import {configStatus,readDemo} from './trading212.js';
import {DEMO_LIMITS,configured as demoConfigured,validateDemoOrder,demoRequest,demoOverview,selectDemoInstruments} from './demo-orders.js';
import {newRealPaper,previewPaper,simulateRealStep,controlPaper,validatePaper} from './real-paper.js';
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
async function actionRealPaper(db,action){
 await ensureRealPaper(db);
 for(let attempt=0;attempt<5;attempt++){
  const {revision,state}=await getRealPaper(db);
  validatePaper(state);
  const out=controlPaper(state,action);
  if(!out.ok)return out;
  const encoded=JSON.stringify(state);let result;
  if(revision===0)result=await db.prepare('INSERT OR IGNORE INTO real_price_paper(id,revision,json) VALUES(1,1,?)').bind(encoded).run();
  else result=await db.prepare('UPDATE real_price_paper SET revision=?,json=? WHERE id=1 AND revision=?').bind(revision+1,encoded,revision).run();
  if(result.meta.changes===1)return {ok:true};
 }
 return {ok:false,reason:'CONCURRENT_UPDATE'};
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
// Demo order desk is manually operated. Each D1 request is reserved before any non-idempotent broker call.
async function ensureDemoDesk(db){
 await db.prepare('CREATE TABLE IF NOT EXISTS demo_order_intents (intent_id TEXT PRIMARY KEY, side TEXT NOT NULL, ticker TEXT NOT NULL, quantity REAL NOT NULL, limit_price REAL NOT NULL, notional REAL NOT NULL, state TEXT NOT NULL, broker_id TEXT, error_code TEXT, created_at TEXT NOT NULL)').run();
 await db.prepare('CREATE TABLE IF NOT EXISTS demo_instrument_catalog (id INTEGER PRIMARY KEY CHECK(id=1), json TEXT NOT NULL, fetched_at TEXT NOT NULL)').run();
}
async function catalogDemo(db,env,refresh=false){
 await ensureDemoDesk(db);
 const row=await db.prepare('SELECT json,fetched_at FROM demo_instrument_catalog WHERE id=1').first();
 const age=row?Date.now()-Date.parse(row.fetched_at):Infinity;
 if(row&&age<10*60000&&!refresh)return {items:JSON.parse(row.json),fetchedAt:row.fetched_at};
 if(row&&age<60*1000&&refresh)return {items:JSON.parse(row.json),fetchedAt:row.fetched_at};
 const instruments=selectDemoInstruments(await demoRequest(env,'/equity/metadata/instruments'));
 const fetchedAt=new Date().toISOString();
 await db.prepare('INSERT INTO demo_instrument_catalog(id,json,fetched_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json,fetched_at=excluded.fetched_at').bind(JSON.stringify(instruments),fetchedAt).run();
 return {items:instruments,fetchedAt};
}
async function demoDeskState(db){
 await ensureDemoDesk(db);
 const rows=await db.prepare('SELECT intent_id,side,ticker,quantity,limit_price,notional,state,broker_id,error_code,created_at FROM demo_order_intents ORDER BY created_at DESC LIMIT 20').all();
 return {orders:rows.results||[],limits:DEMO_LIMITS};
}
async function placeDemoLimit(db,env,input){
 if(!demoConfigured(env))return {ok:false,reason:'DEMO_KEYS_MISSING'};
 if(typeof input?.intentId!=='string'||!/^[0-9a-f-]{36}$/i.test(input.intentId))return {ok:false,reason:'INVALID_INTENT_ID'};
 await ensureDemoDesk(db);
 const cached=await db.prepare('SELECT json,fetched_at FROM demo_instrument_catalog WHERE id=1').first();
 if(!cached||Date.now()-Date.parse(cached.fetched_at)>15*60000)return {ok:false,reason:'REFRESH_INSTRUMENT_CATALOG'};
 const rows=(await db.prepare('SELECT side,ticker,quantity,notional,state FROM demo_order_intents').all()).results||[];
 if(rows.some(x=>x.state==='RESERVED'||x.state==='UNCERTAIN'))return {ok:false,reason:'UNRESOLVED_PREVIOUS_ORDER'};
 const bought=rows.filter(x=>x.side==='BUY'&&x.state!=='REJECTED').reduce((sum,x)=>sum+x.notional,0);
 const ticker=String(input.ticker||'');
 const trackedBuy=rows.filter(x=>x.ticker===ticker&&x.side==='BUY'&&x.state==='ACCEPTED').reduce((sum,x)=>sum+x.quantity,0);
 const trackedSell=rows.filter(x=>x.ticker===ticker&&x.side==='SELL'&&x.state!=='REJECTED').reduce((sum,x)=>sum+x.quantity,0);
 const overview=await demoOverview(env);
 if(overview.pending.length)return {ok:false,reason:'PENDING_BROKER_ORDERS_RECONCILE_FIRST'};
 const assessment=validateDemoOrder(input,{catalog:JSON.parse(cached.json),account:overview.account,
   positions:overview.positions,priorBought:bought,availableToSell:Math.max(0,trackedBuy-trackedSell)});
 if(!assessment.ok)return assessment;
 const inputSide=assessment.side,at=new Date().toISOString();
 // Atomic reservation prevents duplicate intent IDs, concurrent unresolved sends, and budget overrun.
 const reserved=await db.prepare(`INSERT OR IGNORE INTO demo_order_intents(intent_id,side,ticker,quantity,limit_price,notional,state,created_at)
 SELECT ?,?,?,?,?,?,'RESERVED',?
 WHERE NOT EXISTS (SELECT 1 FROM demo_order_intents WHERE state IN ('RESERVED','UNCERTAIN'))
 AND (? <> 'BUY' OR ?+(SELECT COALESCE(SUM(notional),0) FROM demo_order_intents WHERE side='BUY' AND state<>'REJECTED')<=?)`)
  .bind(input.intentId,inputSide,assessment.ticker,assessment.quantity,assessment.limitPrice,assessment.notional,at,
   inputSide,assessment.notional,DEMO_LIMITS.totalBuyBudget).run();
 if(reserved.meta.changes!==1)return {ok:false,reason:'DUPLICATE_OR_CONCURRENT_OR_BUDGET_LIMIT'};
 try{
  // A non-idempotent POST is sent ONCE. Never retry after timeouts, HTTP errors or lost responses.
  const broker=await demoRequest(env,'/equity/orders/limit',{method:'POST',body:{
   ticker:assessment.ticker,quantity:inputSide==='BUY'?assessment.quantity:-assessment.quantity,
   limitPrice:assessment.limitPrice,timeValidity:'DAY'
  }});
  const brokerId=broker?.id;
  if((typeof brokerId!=='number'&&typeof brokerId!=='string')||String(brokerId).length>50)throw Error('BROKER_ACK_UNVERIFIED');
  await db.prepare("UPDATE demo_order_intents SET state='ACCEPTED',broker_id=? WHERE intent_id=?").bind(String(brokerId),input.intentId).run();
  return {ok:true,reason:'BROKER_ORDER_ACCEPTED_NOT_FILLED',intentId:input.intentId,brokerId:String(brokerId),ticker:assessment.ticker,side:inputSide};
 }catch(e){
  const code=String(e?.message||'UNKNOWN').slice(0,80);
  await db.prepare("UPDATE demo_order_intents SET state='UNCERTAIN',error_code=? WHERE intent_id=?").bind(/^DEMO_HTTP_\d+$/.test(code)?code:'BROKER_RESPONSE_UNCERTAIN',input.intentId).run();
  return {ok:false,reason:'BROKER_ORDER_UNCERTAIN_DO_NOT_RETRY',intentId:input.intentId};
 }
}
const LOGIN_PAGE=`<!doctype html><html lang="sk"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Bot · Login</title><style>body{font:16px system-ui;background:#0b1120;color:#ecf2ff;display:grid;min-height:100vh;place-items:center;margin:0}form{background:#151f32;padding:30px;border:1px solid #2a3650;border-radius:18px;width:min(340px,80vw)}input,button{box-sizing:border-box;width:100%;padding:13px;margin-top:12px;border-radius:9px}input{background:#0b1120;color:white;border:1px solid #41506a}button{background:#4d77e8;color:white;border:0;font-weight:bold}small{color:#9ba9c4}#err{color:#ff8b97}</style><form id="f"><h2>Trading Bot Cloud</h2><small>Súkromný paper trading · v0.5.0</small><input type="password" name="password" placeholder="Prístupové heslo" autocomplete="current-password" required><button>Prihlásiť</button><p id="err"></p></form><script src="/login.js"></script></html>`;
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
      if(path==='/health')return json({ok:true,version:'0.5.0-intraday-research'});
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
      if(path==='/api/market-paper/action'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        const body=await req.json().catch(()=>null);
        if(!['start','pause','stop'].includes(body?.action))return json({message:'Unknown action'},400);
        const result=await actionRealPaper(env.DB,body.action);
        return json(result,result.ok?200:409);
      }
      if(path==='/api/market-paper/check'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        const market=await loadMarket(env.DB,env);
        const result=await checkRealPaper(env.DB,market);
        return json(result,result.ok?200:409);
      }
      if(path==='/api/t212/demo/catalog'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        if(!demoConfigured(env))return json({ok:false,reason:'DEMO_KEYS_MISSING'},409);
        try{return json({ok:true,...await catalogDemo(env.DB,env)})}
        catch(e){console.error('Demo catalog error',String(e?.message||e));return json({ok:false,reason:'DEMO_CATALOG_UNAVAILABLE'},503)}
      }
      if(path==='/api/t212/demo/overview'&&req.method==='GET'){
        if(!demoConfigured(env))return json({ok:false,reason:'DEMO_KEYS_MISSING'},409);
        try{
          const data=await demoOverview(env);
          return json({ok:true,account:data.account,positions:data.positions.slice(0,60).map(p=>({
            ticker:String(p.instrument?.ticker||'').slice(0,65),quantity:p.quantity,
            quantityAvailableForTrading:p.quantityAvailableForTrading,
            currentPrice:p.currentPrice
          })),pending:data.pending.slice(0,60).map(p=>({
            id:p.id,ticker:p.ticker,side:p.side,status:p.status,quantity:p.quantity,limitPrice:p.limitPrice
          }))});
        }catch(e){console.error('Demo overview error',String(e?.message||e));return json({ok:false,reason:'DEMO_OVERVIEW_UNAVAILABLE'},503)}
      }
      if(path==='/api/t212/demo/desk'&&req.method==='GET'){
        return json({ok:true,...await demoDeskState(env.DB)});
      }
      if(path==='/api/t212/demo/limit'&&req.method==='POST'){
        if(!sameOrigin(req))return json({message:'Origin mismatch'},403);
        const input=await req.json().catch(()=>null);
        if(!input||typeof input!=='object'||Array.isArray(input))return json({reason:'INVALID_INPUT'},400);
        try{
          const result=await placeDemoLimit(env.DB,env,input);
          return json(result,result.ok?200:409);
        }catch(e){console.error('Demo order preflight error',String(e?.message||e));return json({ok:false,reason:'DEMO_PREFLIGHT_FAILED_NO_ORDER_CONFIRMED'},503)}
      }
      // Manual Demo API reading and manual limit-order testing only. No automated broker orders.
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
        try{
          const refreshed=await refreshMarket(env.DB,env);
          if(!refreshed.skipped){
            const market=await loadMarket(env.DB,env);
            // Close positions on verified prices even while paused. New entries require explicit Start.
            const result=await checkRealPaper(env.DB,market);
            if(!result.ok&&result.reason!=='DUPLICATE_SNAPSHOT'&&result.reason!=='FEED_NOT_READY')console.info('Market paper check skipped:',result.reason);
          }
        }catch(e){console.error('Market watch / USD paper check failed',String(e?.message||e))}
      }
    })());
  }
};
