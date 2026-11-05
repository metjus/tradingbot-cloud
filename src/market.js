// Read-only market-data diagnostics. Quotes never drive orders or the paper engine.
export const WATCH_SYMBOLS=Object.freeze(['AAPL','MSFT','NVDA']);
export const MARKET_PROVIDER='Twelve Data';
const MAX_AGE_MS=20*60*1000;
export function nySession(now=new Date()){
  const parts=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now).filter(p=>p.type!=='literal').map(p=>[p.type,p.value]));
  const m=Number(parts.hour)*60+Number(parts.minute);
  return !['Sat','Sun'].includes(parts.weekday)&&m>=570&&m<960;
}
export function normalizeQuote(symbol,data){
  if(!WATCH_SYMBOLS.includes(symbol))throw Error('Unknown symbol');
  if(!data||data.status==='error'||data.code||data.symbol!==symbol)throw Error('PROVIDER_ERROR');
  const price=Number(data.close);
  const timestamp=Number(data.last_quote_at??data.timestamp);
  if(!Number.isFinite(price)||price<=0||!Number.isFinite(timestamp)||timestamp<1e9)throw Error('INVALID_PRICE_OR_TIME');
  if(data.currency&&data.currency!=='USD')throw Error('UNEXPECTED_CURRENCY');
  // /quote.timestamp may denote a daily candle opening. It is not proof of a live trade.
  const timestampSource=data.last_quote_at!=null?'last_quote_at':'bar_open';
  return {symbol,price,currency:'USD',timestamp,timestampSource,source:MARKET_PROVIDER};
}
export function quoteIsFresh(q,now=Date.now(),maxAgeMs=MAX_AGE_MS){
  if(q?.timestampSource!=='last_quote_at')return false;
  return Number.isFinite(q?.timestamp)&&Number.isFinite(q?.price)&&q.price>0&&now-q.timestamp*1000>=-120000&&now-q.timestamp*1000<=maxAgeMs;
}
const initial=()=>({quotes:[],errors:[],attempts:0,successes:0,lastAttemptAt:null,lastSuccessAt:null});
async function readSnapshot(db){
  try{
    const row=await db.prepare('SELECT json,updated_at FROM market_snapshot WHERE id=1').first();
    return {data:row?{...initial(),...JSON.parse(row.json)}:initial(),updatedAt:row?.updated_at||null};
  }catch(e){
    // A missing table is a fresh installation; other D1 failures must remain visible.
    if(/no such table/i.test(String(e?.message||e)))return {data:initial(),updatedAt:null};
    throw e;
  }
}
export async function loadMarket(db,env,now=new Date()){
  if(!env.TWELVEDATA_API_KEY)return {configured:false,provider:MARKET_PROVIDER,sessionOpen:nySession(now),quotes:[],notice:'Chýba TWELVEDATA_API_KEY v Cloudflare Secrets. Žiadne reálne obchodovanie.'};
  const {data,updatedAt}=await readSnapshot(db);
  const sessionOpen=nySession(now),quotes=(data.quotes||[]).map(q=>({...q,fresh:sessionOpen&&quoteIsFresh(q,now.getTime())}));
  const usable=sessionOpen&&WATCH_SYMBOLS.every(sym=>quotes.some(q=>q.symbol===sym&&q.fresh))&&!(data.errors||[]).length;
  return {configured:true,provider:MARKET_PROVIDER,sessionOpen,quotes,errors:data.errors||[],updatedAt,
    lastAttemptAt:data.lastAttemptAt,lastSuccessAt:data.lastSuccessAt,attempts:data.attempts,successes:data.successes,
    diagnostic:!sessionOpen?'MARKET_CLOSED':usable?'FEED_READY':!data.lastAttemptAt?'AWAITING_FIRST_CHECK':(data.errors||[]).length?'PROVIDER_OR_DATA_ERROR':'STALE_OR_UNVERIFIED',
    readyForPaper:usable,notice:'Len sledovanie USD cien. Syntetické obchody zostávajú nezmenené. Kalendár burzových sviatkov nie je implementovaný.'};
}
export async function refreshMarket(db,env,now=new Date(),fetcher=fetch){
  if(!env.TWELVEDATA_API_KEY)return {skipped:true,reason:'NOT_CONFIGURED'};
  if(!nySession(now))return {skipped:true,reason:'MARKET_CLOSED'};
  await db.prepare('CREATE TABLE IF NOT EXISTS market_snapshot (id INTEGER PRIMARY KEY CHECK (id=1), json TEXT NOT NULL, updated_at TEXT NOT NULL)').run();
  const {data:prior}=await readSnapshot(db);
  const current=initial(),errors=[],quotes=[];
  for(const symbol of WATCH_SYMBOLS){
    try{
      const url='https://api.twelvedata.com/quote?symbol='+encodeURIComponent(symbol);
      const response=await fetcher(url,{headers:{'Authorization':'apikey '+env.TWELVEDATA_API_KEY},signal:AbortSignal.timeout(7000)});
      if(!response.ok)throw Error('HTTP_'+response.status);
      const raw=await response.json();
      if(raw?.status==='error'||raw?.code)throw Error('PROVIDER_'+String(raw.code||'ERROR'));
      const quote=normalizeQuote(symbol,raw);
      if(!quoteIsFresh(quote,now.getTime()))throw Error(quote.timestampSource==='bar_open'?'BAR_TIMESTAMP_NOT_LIVE':'STALE_QUOTE');
      quotes.push(quote);
    }catch(e){
      const msg=String(e?.message||e).slice(0,90);
      errors.push(symbol+': '+msg);
      const old=(prior.quotes||[]).find(q=>q.symbol===symbol);
      if(old)quotes.push(old); // old quote must stay explicitly stale based on timestamp.
    }
  }
  const success=errors.length===0&&quotes.length===WATCH_SYMBOLS.length;
  const snapshot={quotes,errors,attempts:(prior.attempts||0)+1,successes:(prior.successes||0)+(success?1:0),
    lastAttemptAt:now.toISOString(),lastSuccessAt:success?now.toISOString():prior.lastSuccessAt||null};
  const updatedAt=now.toISOString();
  await db.prepare('INSERT INTO market_snapshot(id,json,updated_at) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json,updated_at=excluded.updated_at')
    .bind(JSON.stringify(snapshot),updatedAt).run();
  return {success,errors,quotes:quotes.length};
}
