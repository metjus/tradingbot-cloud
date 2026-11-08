// Manual-only, DEMO-only limit orders for end-to-end broker integration tests.
// No live hostname, auto orders, retries, or credentials in persisted state.
const BASE='https://demo.trading212.com/api/v0';
export const DEMO_LIMITS=Object.freeze({perOrder:25,totalBuyBudget:100,maxQuantity:100,limitPriceMax:100000});
const finite=v=>typeof v==='number'&&Number.isFinite(v);
const rounded=n=>Math.round(n*1e6)/1e6;
export const configured=env=>Boolean(env.T212_DEMO_API_KEY&&env.T212_DEMO_API_SECRET);
export function validateDemoOrder(input,{catalog=[],account=null,positions=[],priorBought=0,availableToSell=0}={}){
 const {ticker,side,quantity,limitPrice}=input||{};
 if(!['BUY','SELL'].includes(side))return {ok:false,reason:'INVALID_SIDE'};
 if(typeof ticker!=='string'||ticker.length<2||ticker.length>65||!/^[A-Za-z0-9_.-]+$/.test(ticker))return {ok:false,reason:'INVALID_TICKER_FORMAT'};
 // T212 uses case-sensitive venue suffixes (e.g. lowercase 'l' for London).
 // The exact ticker must also exist in the broker-fetched allowlisted catalog below.
 if(!finite(quantity)||!finite(limitPrice)||quantity<=0||quantity>DEMO_LIMITS.maxQuantity||
  limitPrice<=0||limitPrice>DEMO_LIMITS.limitPriceMax||
  Math.abs(quantity*10000-Math.round(quantity*10000))>1e-7)return {ok:false,reason:'INVALID_SIZE_OR_PRICE'};
 const instrument=catalog.find(x=>x.ticker===ticker);
 if(!instrument||!['STOCK','ETF'].includes(instrument.type))return {ok:false,reason:'TICKER_NOT_IN_VERIFIED_CATALOG'};
 const currency=account?.currency;
 if(!currency||instrument.currencyCode!==currency)return {ok:false,reason:'ACCOUNT_CURRENCY_MISMATCH'};
 const notional=rounded(quantity*limitPrice);
 if(side==='BUY'){
  if(notional>DEMO_LIMITS.perOrder||notional<1)return {ok:false,reason:'ORDER_CAP_25'};
  if(priorBought+notional>DEMO_LIMITS.totalBuyBudget+.0001)return {ok:false,reason:'LIFETIME_TEST_BUDGET_100'};
  if(!finite(account.availableToTrade)||account.availableToTrade<notional*1.02)return {ok:false,reason:'INSUFFICIENT_DEMO_CASH'};
  if(positions.some(p=>(p.instrument?.ticker||p.ticker)===ticker))return {ok:false,reason:'EXISTING_POSITION_IN_TICKER'};
 }else{
  if(availableToSell+1e-7<quantity)return {ok:false,reason:'SELL_EXCEEDS_TRACKED_BUYS'};
  const pos=positions.find(p=>(p.instrument?.ticker||p.ticker)===ticker);
  if(!pos||!finite(pos.quantityAvailableForTrading)||pos.quantityAvailableForTrading+1e-7<quantity)
   return {ok:false,reason:'SELL_EXCEEDS_BROKER_POSITION'};
 }
 return {ok:true,ticker,side,quantity,limitPrice,notional,currency,instrumentName:instrument.name};
}
export async function demoRequest(env,path,{method='GET',body=null}={},fetcher=fetch){
 if(!configured(env))throw Error('DEMO_KEYS_MISSING');
 if(!path.startsWith('/equity/')||path.includes('..'))throw Error('INVALID_DEMO_PATH');
 const headers={Authorization:'Basic '+btoa(env.T212_DEMO_API_KEY+':'+env.T212_DEMO_API_SECRET),Accept:'application/json'};
 if(body){headers['Content-Type']='application/json'}
 const response=await fetcher(BASE+path,{method,headers,body:body?JSON.stringify(body):undefined,signal:AbortSignal.timeout(8000)});
 if(!response.ok)throw Error('DEMO_HTTP_'+response.status);
 return response.json();
}
export function selectDemoInstruments(raw){
 if(!Array.isArray(raw))throw Error('INVALID_INSTRUMENT_LIST');
 const matching=raw.filter(x=>x&&typeof x.ticker==='string'&&typeof x.name==='string'&&
  ['STOCK','ETF'].includes(x.type)&&/^[A-Z]{3}$/.test(x.currencyCode||'')&&
  ((x.type==='STOCK'&&/(^|[^a-z])(apple|microsoft|nvidia)([^a-z]|$)/i.test(x.name))||
   (x.type==='ETF'&&/s&p.?500|nasdaq.?100|msci.world|physical.gold|gold.et[cfn]/i.test(x.name))));
 // Keep a balanced currency sample, otherwise USD listings may hide all EUR alternatives.
 const seen=new Set(),balanced=[];
 for(const currency of ['EUR','USD','GBP']){
  for(const item of matching.filter(x=>x.currencyCode===currency).slice(0,30)){
   if(!seen.has(item.ticker)){seen.add(item.ticker);balanced.push(item)}
  }
 }
 return balanced.map(x=>({ticker:x.ticker,name:x.name.slice(0,100),currencyCode:x.currencyCode,type:x.type}));
}
export async function demoOverview(env,fetcher=fetch){
 const [summary,positions,pending]=await Promise.all([
  demoRequest(env,'/equity/account/summary',{},fetcher),
  demoRequest(env,'/equity/positions',{},fetcher),
  demoRequest(env,'/equity/orders',{},fetcher)
 ]);
 if(!summary||typeof summary.currency!=='string'||!Array.isArray(positions)||!Array.isArray(pending))throw Error('INVALID_BROKER_DATA');
 return {account:{currency:summary.currency,availableToTrade:summary.cash?.availableToTrade,
   totalValue:summary.totalValue},positions,pending};
}
