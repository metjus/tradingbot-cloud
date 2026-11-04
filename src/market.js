// Read-only U.S. market watch. No orders or paper executions use these quotes.
export const WATCH_SYMBOLS = Object.freeze(['AAPL','MSFT','NVDA']);
export const MARKET_PROVIDER = 'Twelve Data';
export function nySession(now=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
  const m=Number(parts.hour)*60+Number(parts.minute);
  return !['Sat','Sun'].includes(parts.weekday)&&m>=570&&m<960;
}
export function normalizeQuote(symbol,data){
  if(!WATCH_SYMBOLS.includes(symbol))throw Error('Unknown symbol');
  if(!data || data.status==='error'||data.code || data.symbol!==symbol)throw Error('Provider returned an error or wrong ticker');
  const price=Number(data.close);
  const timestamp=Number(data.timestamp);
  if(!Number.isFinite(price)||price<=0||!Number.isFinite(timestamp)||timestamp<1e9)throw Error('Missing price or valid source timestamp');
  if(data.currency && data.currency!=='USD')throw Error('Unexpected quote currency');
  return {symbol,price,currency:'USD',timestamp,source:MARKET_PROVIDER};
}
export function quoteIsFresh(q,now=Date.now(),maxAgeMs=20*60000){
  return Number.isFinite(q?.timestamp)&&Number.isFinite(q?.price)&&q.price>0&&now-q.timestamp*1000>=-120000&&now-q.timestamp*1000<=maxAgeMs;
}
export async function loadMarket(db,env){
  if(!env.TWELVEDATA_API_KEY)return {configured:false,provider:MARKET_PROVIDER,sessionOpen:nySession(),quotes:[],notice:'Pridaj TWELVEDATA_API_KEY do Cloudflare Secrets. Reálne obchodovanie je vypnuté.'};
  const row=await db.prepare('SELECT json,updated_at FROM market_snapshot WHERE id=1').first().catch(()=>null);
  const saved=row?JSON.parse(row.json):{quotes:[],errors:[]};
  return {configured:true,provider:MARKET_PROVIDER,sessionOpen:nySession(),quotes:(saved.quotes||[]).map(q=>({...q,fresh:quoteIsFresh(q)})),errors:saved.errors||[],updatedAt:row?.updated_at||null,notice:'Len sledovanie cien v USD. Nie sú používané na obchodovanie.'};
}
export async function refreshMarket(db,env,now=new Date()){
  if(!env.TWELVEDATA_API_KEY||!nySession(now))return {skipped:true};
  await db.prepare('CREATE TABLE IF NOT EXISTS market_snapshot (id INTEGER PRIMARY KEY CHECK (id=1), json TEXT NOT NULL, updated_at TEXT NOT NULL)').run();
  const prior=await db.prepare('SELECT json FROM market_snapshot WHERE id=1').first();
  const prev=prior?JSON.parse(prior.json):{quotes:[]};
  const collected=[],errors=[];
  for(const symbol of WATCH_SYMBOLS){
    try{
      const url='https://api.twelvedata.com/quote?symbol='+encodeURIComponent(symbol)+'&apikey='+encodeURIComponent(env.TWELVEDATA_API_KEY);
      const response=await fetch(url,{signal:AbortSignal.timeout(7000)});
      if(!response.ok)throw Error('HTTP '+response.status);
      const raw=await response.json();
      const quote=normalizeQuote(symbol,raw);
      if(!quoteIsFresh(quote,now.getTime()))throw Error('Stale quote');
      collected.push(quote);
    }catch(e){errors.push(symbol+': '+String(e.message||e).slice(0,80));const old=(prev.quotes||[]).find(x=>x.symbol===symbol);if(old)collected.push(old)}
  }
  const data=JSON.stringify({quotes:collected,errors});
  const updatedAt=now.toISOString();
  await db.prepare('INSERT INTO market_snapshot(id,json,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json,updated_at=excluded.updated_at').bind(data,updatedAt).run();
  return {quotes:collected.length,errors};
}
