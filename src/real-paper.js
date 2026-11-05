// USD-only isolated market-price paper sandbox; never calls a broker.
import {WATCH_SYMBOLS,quoteIsFresh,nySession} from './market.js';
export const REAL_PAPER_LIMITS=Object.freeze({startingUsd:500,maxPositions:2,maxAllocationPct:25,riskPct:.5,stopPct:.8,targetPct:.6,holdMinutes:15,spreadBps:10,slippageBps:5,feeUsd:0,lossLimitPct:2});
const fix=n=>Math.round(n*1e6)/1e6;
const isNum=n=>typeof n==='number'&&Number.isFinite(n);
export const newRealPaper=()=>({currency:'USD',initial:500,cash:500,positions:{},trades:[],events:[],lastSnapshotAt:null,lastQuotes:{},halted:false,paused:true,createdAt:new Date().toISOString()});
const equity=(s,prices)=>fix(s.cash+Object.entries(s.positions).reduce((sum,[symbol,p])=>sum+p.qty*(prices[symbol]??p.lastMark),0));
const entryCost=(price)=>price*(1+(REAL_PAPER_LIMITS.spreadBps/2+REAL_PAPER_LIMITS.slippageBps)/10000);
const exitPrice=(price)=>price*(1-(REAL_PAPER_LIMITS.spreadBps/2+REAL_PAPER_LIMITS.slippageBps)/10000);
const log=(s,event,now)=>{s.events.unshift({at:now.toISOString(),event});s.events=s.events.slice(0,35)};
export function quoteGate(m,now=new Date()){
 if(!nySession(now)||!m?.sessionOpen)return {ok:false,reason:'MARKET_CLOSED'};
 if(!m?.readyForPaper||m.diagnostic!=='FEED_READY')return {ok:false,reason:'FEED_NOT_READY'};
 if(!m.updatedAt||!Number.isFinite(Date.parse(m.updatedAt))||now.getTime()-Date.parse(m.updatedAt)>120000||Date.parse(m.updatedAt)>now.getTime()+10000)return {ok:false,reason:'SNAPSHOT_TOO_OLD'};
 const quotes={};
 for(const sym of WATCH_SYMBOLS){
  const q=m.quotes?.find(x=>x.symbol===sym);
  if(!q||q.currency!=='USD'||!quoteIsFresh(q,now.getTime(),120000)||!isNum(q.price)||q.price<=0||q.fresh!==true)return {ok:false,reason:'INVALID_'+sym};
  quotes[sym]=q.price;
 }
 return {ok:true,prices:quotes};
}
export function validatePaper(s){
 if(!s||s.currency!=='USD'||s.initial!==500||!isNum(s.cash)||s.cash<-.0001||Object.keys(s.positions).length>2)throw Error('PAPER_STATE_INVALID');
 const cost=Object.values(s.positions).reduce((sum,p)=>sum+p.cost,0);
 if(cost>500.01||!Object.values(s.positions).every(p=>isNum(p.qty)&&p.qty>0&&isNum(p.cost)&&p.cost>0))throw Error('PAPER_CAPITAL_INVALID');
}
export function simulateRealStep(s,m,now=new Date()){
 validatePaper(s);
 const gate=quoteGate(m,now);
 if(!gate.ok)return {ok:false,reason:gate.reason};
 if(s.halted)return {ok:false,reason:'HALTED'};
 if(s.lastSnapshotAt===m.updatedAt)return {ok:false,reason:'DUPLICATE_SNAPSHOT'};
 const prices=gate.prices,stamp=now.toISOString();
 // Close only on verified market prices. No synthetic fallback on missing data.
 for(const [sym,p] of Object.entries(s.positions)){
  const mark=prices[sym],age=now.getTime()-Date.parse(p.openedAt);
  const reason=mark<=p.stop?'STOP_LOSS':mark>=p.target?'TAKE_PROFIT':age>=REAL_PAPER_LIMITS.holdMinutes*60000?'TIME_EXIT':null;
  p.lastMark=mark;
  if(!reason)continue;
  const proceeds=fix(p.qty*exitPrice(mark)-REAL_PAPER_LIMITS.feeUsd);
  s.cash=fix(s.cash+proceeds);
  const pnl=fix(proceeds-p.cost);
  s.trades.unshift({symbol:sym,pnl,reason,openedAt:p.openedAt,closedAt:stamp,entry:p.entry,exit:exitPrice(mark),qty:p.qty});
  s.trades=s.trades.slice(0,150);delete s.positions[sym];
  log(s,'CLOSE '+sym+' '+reason,now);
 }
 const eq=equity(s,prices);
 if(eq<=s.initial*(1-REAL_PAPER_LIMITS.lossLimitPct/100)){
  s.halted=true;s.paused=true;log(s,'DAILY_LIMIT_SIMPLIFIED_HALTED',now);
 }
 // Explicitly no auto-entry strategy. Preserve old price and show directional signals.
 const signals=WATCH_SYMBOLS.map(sym=>({symbol:sym,price:prices[sym],changePct:isNum(s.lastQuotes[sym])?fix((prices[sym]/s.lastQuotes[sym]-1)*100):null,action:'HOLD'}));
 s.lastQuotes={...prices};s.lastSnapshotAt=m.updatedAt;
 validatePaper(s);
 return {ok:true,signals,eq};
}
export function previewPaper(s,m,now=new Date()){
 const gate=quoteGate(m,now);
 const prices=gate.ok?gate.prices:s.lastQuotes;
 return {mode:'MARKET_PRICE_PAPER_PREVIEW',currency:'USD',initial:s.initial,cash:fix(s.cash),equity:equity(s,prices),pnl:fix(equity(s,prices)-s.initial),positions:Object.entries(s.positions).map(([symbol,p])=>({symbol,qty:p.qty,entry:p.entry,mark:prices[symbol]??p.lastMark})),trades:s.trades.slice(0,20),events:s.events.slice(0,15),canCheck:gate.ok,reason:gate.ok?'FEED_OK':gate.reason,paused:s.paused,halted:s.halted,lastSnapshotAt:s.lastSnapshotAt,assumptions:REAL_PAPER_LIMITS};
}
