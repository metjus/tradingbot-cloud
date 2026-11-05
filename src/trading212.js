// Strictly read-only Trading 212 DEMO connector. Never use live host or order endpoints.
const BASE='https://demo.trading212.com/api/v0';
export function configStatus(env){
 const key=!!env.T212_DEMO_API_KEY,secret=!!env.T212_DEMO_API_SECRET;
 return {configured:key&&secret,mode:'DEMO_READ_ONLY',reason:key!==secret?'INCOMPLETE_CREDENTIALS':key?'READY':'NOT_CONFIGURED'};
}
export async function readDemo(env,fetcher=fetch){
 const configuration=configStatus(env);
 if(!configuration.configured)return {...configuration,connected:false,summary:null,positions:[],checkedAt:null};
 const token=btoa(env.T212_DEMO_API_KEY+':'+env.T212_DEMO_API_SECRET);
 const read=async path=>{
   const res=await fetcher(BASE+path,{method:'GET',headers:{Authorization:'Basic '+token,Accept:'application/json'},signal:AbortSignal.timeout(8000)});
   if(!res.ok){const error=new Error('DEMO_HTTP_'+res.status);error.status=res.status;throw error}
   return res.json();
 };
 try{
   const summary=await read('/equity/account/summary');
   const positions=await read('/equity/positions');
   if(!summary||typeof summary!=='object'||!Array.isArray(positions))throw Error('DEMO_INVALID_RESPONSE');
   const account={
     currency:typeof summary.currency==='string'?summary.currency:null,
     totalValue:Number.isFinite(summary.totalValue)?summary.totalValue:null,
     availableToTrade:Number.isFinite(summary.cash?.availableToTrade)?summary.cash.availableToTrade:null,
     investedValue:Number.isFinite(summary.investments?.currentValue)?summary.investments.currentValue:null,
     unrealizedProfitLoss:Number.isFinite(summary.investments?.unrealizedProfitLoss)?summary.investments.unrealizedProfitLoss:null
   };
   const limited=positions.slice(0,100).map(p=>({
     ticker:String(p.instrument?.ticker||p.instrument?.name||'Neznámy').slice(0,90),
     quantity:Number.isFinite(p.quantity)?p.quantity:null,
     price:Number.isFinite(p.currentPrice)?p.currentPrice:null,
     currency:String(p.instrument?.currencyCode||summary.currency||'').slice(0,8)
   }));
   return {...configuration,connected:true,checkedAt:new Date().toISOString(),account,positions:limited,positionCount:positions.length};
 }catch(e){
   const code=String(e?.message||'DEMO_UNKNOWN_ERROR');
   return {...configuration,connected:false,checkedAt:new Date().toISOString(),account:null,positions:[],error:/^DEMO_(HTTP_\\d+|INVALID_RESPONSE)$/.test(code)?code:'DEMO_NETWORK_OR_RESPONSE_ERROR'};
 }
}
