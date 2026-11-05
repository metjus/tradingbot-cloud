// Isolated USD paper account using observed market prices. NO broker integration.
import {WATCH_SYMBOLS,quoteIsFresh,nySession} from './market.js';
export const REAL_PAPER_LIMITS=Object.freeze({startingUsd:500,maxPositions:2,maxAllocationPct:25,riskPct:.5,stopPct:.8,targetPct:.6,holdMinutes:15,spreadBps:10,slippageBps:5,feeUsd:0,lossLimitPct:2,momentumThresholdPct:.25,cooldownMinutes:20});
const round=n=>Math.round(n*1e6)/1e6;
const isNum=n=>typeof n==='number'&&Number.isFinite(n);
const log=(s,event,now)=>{s.events.unshift({at:now.toISOString(),event});s.events=s.events.slice(0,50)};
export const newRealPaper=()=>({currency:'USD',initial:500,cash:500,positions:{},trades:[],events:[],lastSnapshotAt:null,lastQuotes:{},samples:{},halted:false,paused:true,pauseRequested:false,createdAt:new Date().toISOString(),day:null,dayEquity:500,cooldowns:{}});
export function migratePaper(s){
 if(!s.samples||typeof s.samples!=='object')s.samples={};
 if(!s.cooldowns||typeof s.cooldowns!=='object')s.cooldowns={};
 if(!s.lastQuotes||typeof s.lastQuotes!=='object')s.lastQuotes={};
 if(!Array.isArray(s.trades))s.trades=[];
 if(!Array.isArray(s.events))s.events=[];
 if(!isNum(s.dayEquity))s.dayEquity=s.initial;
 if(typeof s.paused!=='boolean')s.paused=true;
 if(typeof s.pauseRequested!=='boolean')s.pauseRequested=false;
 return s;
}
const buyPx=price=>price*(1+(REAL_PAPER_LIMITS.spreadBps/2+REAL_PAPER_LIMITS.slippageBps)/10000);
const sellPx=price=>price*(1-(REAL_PAPER_LIMITS.spreadBps/2+REAL_PAPER_LIMITS.slippageBps)/10000);
const markedPx=(s,sym,prices)=>prices[sym]??s.lastQuotes[sym]??s.positions[sym]?.lastMark;
export function paperEquity(s,prices={}){
 let eq=s.cash;
 for(const [sym,p] of Object.entries(s.positions)){
  const px=markedPx(s,sym,prices);
  if(!isNum(px)||px<=0)throw Error('MISSING_MARK_PRICE');
  eq+=p.qty*sellPx(px)-REAL_PAPER_LIMITS.feeUsd;
 }
 return round(eq);
}
export function quoteGate(m,now=new Date()){
 if(!nySession(now)||!m?.sessionOpen)return {ok:false,reason:'MARKET_CLOSED'};
 if(!m?.readyForPaper||m.diagnostic!=='FEED_READY')return {ok:false,reason:'FEED_NOT_READY'};
 const at=Date.parse(m.updatedAt||'');
 if(!Number.isFinite(at)||now.getTime()-at>7*60000||at>now.getTime()+10000)return {ok:false,reason:'SNAPSHOT_TOO_OLD'};
 const quotes={};
 for(const sym of WATCH_SYMBOLS){
  const q=m.quotes?.find(x=>x.symbol===sym);
  if(!q||q.currency!=='USD'||!quoteIsFresh(q,now.getTime(),7*60000)||!isNum(q.price)||q.price<=0||q.fresh!==true)return {ok:false,reason:'INVALID_'+sym};
  quotes[sym]=q.price;
 }
 return {ok:true,prices:quotes};
}
export function validatePaper(s){
 if(!s||s.currency!=='USD'||s.initial!==500||!isNum(s.cash)||s.cash<-.0001||Object.keys(s.positions).length>REAL_PAPER_LIMITS.maxPositions)throw Error('PAPER_STATE_INVALID');
 let cost=0;
 for(const [symbol,p] of Object.entries(s.positions)){
  if(!WATCH_SYMBOLS.includes(symbol)||!isNum(p.qty)||p.qty<=0||!isNum(p.cost)||p.cost<=0||!isNum(p.entry)||p.entry<=0)throw Error('PAPER_CAPITAL_INVALID');
  if(p.cost>s.initial*REAL_PAPER_LIMITS.maxAllocationPct/100+0.01)throw Error('POSITION_ALLOCATION_INVALID');
  cost+=p.cost;
 }
 if(cost>s.initial+0.01)throw Error('PAPER_CAPITAL_INVALID');
}
export function controlPaper(s,action,now=new Date()){
 migratePaper(s);validatePaper(s);
 if(action==='start'){
  if(s.halted)return {ok:false,reason:'RISK_HALT'};
  s.paused=false;s.pauseRequested=false;log(s,'START market paper (virtual only)',now);
 }else if(action==='pause'){
  s.paused=true;s.pauseRequested=Object.keys(s.positions).length>0;
  log(s,s.pauseRequested?'PAUSE: close positions by TP/SL/time, no entries':'PAUSED',now);
 }else if(action==='stop'){
  // Never claim an immediate close without a verified executable price.
  s.paused=true;s.halted=true;s.pauseRequested=Object.keys(s.positions).length>0;
  log(s,'EMERGENCY LOCK: close on next valid market snapshot (not guaranteed instant)',now);
 }else return {ok:false,reason:'UNKNOWN_ACTION'};
 return {ok:true};
}
function close(s,sym,market,reason,now){
 const p=s.positions[sym],exit=round(sellPx(market)),proceeds=round(p.qty*exit-REAL_PAPER_LIMITS.feeUsd);
 s.cash=round(s.cash+proceeds);
 const pnl=round(proceeds-p.cost);
 s.trades.unshift({symbol:sym,pnl,reason,openedAt:p.openedAt,closedAt:now.toISOString(),entry:p.entry,exit,qty:p.qty,cost:p.cost});
 s.trades=s.trades.slice(0,200);delete s.positions[sym];s.cooldowns[sym]=now.toISOString();
 log(s,'CLOSE '+sym+' '+reason+' P/L '+pnl.toFixed(2)+' USD',now);
}
function signal(s,symbol,price){
 const data=s.samples[symbol]||[];
 if(data.length<3)return {symbol,price,action:'HOLD',reason:'NEED_3_SAMPLES'};
 const [a,b,c]=data.slice(-3).map(x=>x.price);
 const momentum=round((c/a-1)*100);
 const pass=a<b&&b<c&&momentum>=REAL_PAPER_LIMITS.momentumThresholdPct;
 return {symbol,price,momentumPct:momentum,action:pass?'BUY':'HOLD',reason:pass?'MOMENTUM_3_POINTS':'NO_SIGNAL'};
}
function open(s,sym,price,now){
 // Avoid new positions late in the US session, when a 15-minute time exit could miss the close.
 const clock=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now);
 const hour=Number(clock.find(p=>p.type==='hour')?.value),minute=Number(clock.find(p=>p.type==='minute')?.value);
 if(hour*60+minute>=15*60+40)return 'SESSION_END_BUFFER';
 if(s.paused||s.halted||s.pauseRequested||sym in s.positions)return 'NOT_ALLOWED';
 if(Object.keys(s.positions).length>=REAL_PAPER_LIMITS.maxPositions)return 'POSITION_LIMIT';
 if(s.cooldowns[sym]&&now.getTime()-Date.parse(s.cooldowns[sym])<REAL_PAPER_LIMITS.cooldownMinutes*60000)return 'COOLDOWN';
 const entry=buyPx(price),stop=price*(1-REAL_PAPER_LIMITS.stopPct/100);
 const unitRisk=entry-sellPx(stop);
 if(unitRisk<=0)return 'INVALID_RISK';
 const qty=Math.floor(Math.min(s.initial*REAL_PAPER_LIMITS.riskPct/100/unitRisk,
  s.initial*REAL_PAPER_LIMITS.maxAllocationPct/100/entry,
  (s.cash-REAL_PAPER_LIMITS.feeUsd)/entry)*10000)/10000;
 const cost=round(qty*entry+REAL_PAPER_LIMITS.feeUsd);
 if(qty<=0||cost>s.cash+0.000001)return 'CAPITAL_LIMIT';
 s.cash=round(s.cash-cost);
 s.positions[sym]={qty,entry:round(entry),cost,stop:round(stop),target:round(price*(1+REAL_PAPER_LIMITS.targetPct/100)),openedAt:now.toISOString(),lastMark:price};
 log(s,'BUY '+sym+' '+qty+' @ '+entry.toFixed(2)+' USD virtual',now);
 return 'OPENED';
}
export function simulateRealStep(s,m,now=new Date()){
 migratePaper(s);validatePaper(s);
 const gate=quoteGate(m,now);
 if(!gate.ok)return {ok:false,reason:gate.reason};
 if(s.lastSnapshotAt===m.updatedAt)return {ok:false,reason:'DUPLICATE_SNAPSHOT'};
 const prices=gate.prices,day=now.toISOString().slice(0,10);
 if(s.day!==day){s.day=day;s.dayEquity=paperEquity(s,prices);log(s,'NEW_UTC_DAY baseline '+s.dayEquity.toFixed(2),now)}
 // Verified snapshots only. Close open positions before evaluating new entries.
 for(const [sym,p] of Object.entries(s.positions)){
  const mark=prices[sym],age=now.getTime()-Date.parse(p.openedAt);
  p.lastMark=mark;
  const reason=s.halted?'EMERGENCY_EXIT':mark<=p.stop?'STOP_LOSS':mark>=p.target?'TAKE_PROFIT':age>=REAL_PAPER_LIMITS.holdMinutes*60000?'TIME_EXIT':null;
  if(reason)close(s,sym,mark,reason,now);
 }
 if(paperEquity(s,prices)<=s.dayEquity*(1-REAL_PAPER_LIMITS.lossLimitPct/100)){
  s.halted=true;s.paused=true;s.pauseRequested=Object.keys(s.positions).length>0;log(s,'DAILY_LOSS_LIMIT: halt new entries',now);
 }
 for(const sym of WATCH_SYMBOLS){
  const data=s.samples[sym]||[];
  data.push({at:m.updatedAt,price:prices[sym]});
  s.samples[sym]=data.slice(-12);
 }
 const signals=WATCH_SYMBOLS.map(sym=>signal(s,sym,prices[sym]));
 // Entry strategy is opt-in and never enabled during migration. Closes remain monitored while paused.
 if(!s.paused&&!s.halted){
  for(const sig of signals)if(sig.action==='BUY'){
   const result=open(s,sig.symbol,sig.price,now);
   sig.result=result;
  }
 }
 if(s.pauseRequested&&Object.keys(s.positions).length===0){s.pauseRequested=false;log(s,'ALL_POSITIONS_DRAINED',now)}
 s.lastQuotes={...prices};s.lastSnapshotAt=m.updatedAt;
 validatePaper(s);
 return {ok:true,signals,eq:paperEquity(s,prices)};
}
export function previewPaper(s,m,now=new Date()){
 migratePaper(s);
 const gate=quoteGate(m,now),prices=gate.ok?gate.prices:s.lastQuotes;
 let eq;
 try{eq=paperEquity(s,prices)}catch{eq=s.cash}
 const trades=s.trades||[];
 const profit=trades.reduce((v,t)=>v+Math.max(t.pnl,0),0),loss=trades.reduce((v,t)=>v+Math.max(-t.pnl,0),0);
 return {mode:'MARKET_PRICE_PAPER',currency:'USD',initial:s.initial,cash:round(s.cash),equity:eq,pnl:round(eq-s.initial),
  positions:Object.entries(s.positions).map(([symbol,p])=>({symbol,qty:p.qty,entry:p.entry,mark:markedPx(s,symbol,prices),openedAt:p.openedAt,stop:p.stop,target:p.target})),
  trades:trades.slice(0,20),tradeCount:trades.length,profitFactor:loss?round(profit/loss):profit?'BEZ_STRAT':'NEDOSTATOK_DAT',
  events:s.events.slice(0,15),canCheck:gate.ok,reason:gate.ok?'FEED_OK':gate.reason,paused:s.paused,halted:s.halted,pauseRequested:!!s.pauseRequested,
  lastSnapshotAt:s.lastSnapshotAt,assumptions:REAL_PAPER_LIMITS};
}
