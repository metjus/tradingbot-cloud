// Deterministic synthetic-paper simulator. No market feeds, broker, or AI integration.
export const SYMBOLS = ['SIM-ALPHA', 'SIM-BETA', 'SIM-GAMMA'];
export const VERSION = '0.6.2-cloud';
export const POLICY = Object.freeze({stop_loss_pct:0.8,take_profit_pct:0.6,max_hold_minutes:15});
const START_PRICES = [100, 55, 180];
const round = (n, places=8) => Math.round(n * (10**places)) / (10**places);
const nowIso = () => new Date().toISOString();
const valid = n => typeof n === 'number' && Number.isFinite(n);
export function fresh(capital=500) {
  if (!valid(capital) || capital < 10 || capital > 1_000_000) throw Error('Invalid paper capital');
  const prices = Object.fromEntries(SYMBOLS.map((s,i) => [s, START_PRICES[i]]));
  return {initial:capital,cash:capital,prices,history:Object.fromEntries(SYMBOLS.map(s=>[s,Array(35).fill(prices[s])])),positions:{},trades:[],events:[],equity_history:[],
    running:false,pause_requested:false,halted:false,tick:0,seed:42,day:new Date().toISOString().slice(0,10),day_equity:capital,
    limits:{risk_per_trade_pct:.5,daily_loss_pct:2,stop_loss_pct:POLICY.stop_loss_pct,take_profit_pct:POLICY.take_profit_pct,max_hold_minutes:POLICY.max_hold_minutes,max_positions:2,max_allocation_pct:25}};
}
export function equity(s){return s.cash + Object.entries(s.positions).reduce((v,[k,p])=>v+p.qty*s.prices[k],0)}
export function invested(s){return Object.entries(s.positions).reduce((v,[k,p])=>v+p.qty*s.prices[k],0)}
function log(s,kind,message){s.events.unshift({at:nowIso(),kind,message});s.events=s.events.slice(0,100)}
function rng(s){s.seed=(Math.imul(1664525,s.seed)+1013904223)>>>0;return (s.seed+.5)/4294967296}
function normal(s){return Math.sqrt(-2*Math.log(rng(s)))*Math.cos(2*Math.PI*rng(s))}
function lossHit(s){return equity(s)<=s.day_equity*(1-s.limits.daily_loss_pct/100)}
function close(s,sym,reason){
  const p=s.positions[sym];if(!p)return false;
  const market=s.prices[sym]; const pnl=round(p.qty*(market-p.entry));
  s.cash=round(s.cash+p.qty*market); delete s.positions[sym];
  s.trades.unshift({symbol:sym,entry:p.entry,exit:round(market,5),qty:p.qty,pnl:round(pnl,4),reason,at:nowIso()});
  s.trades=s.trades.slice(0,300);log(s,'sell',`SELL ${sym}: ${pnl.toFixed(2)} EUR (${reason})`);return true;
}
function applyPolicy(s){
  s.limits.stop_loss_pct=POLICY.stop_loss_pct;
  s.limits.take_profit_pct=POLICY.take_profit_pct;
  s.limits.max_hold_minutes=POLICY.max_hold_minutes;
  // Update outstanding synthetic targets once when migrating from v0.2.0.
  for(const p of Object.values(s.positions)){
    if(!p.policy_version){
      p.stop=round(p.entry*(1-POLICY.stop_loss_pct/100));
      p.target=round(p.entry*(1+POLICY.take_profit_pct/100));
      p.policy_version='0.2.1';
    }
  }
}
export function checkEntry(s,sym){
  if(!s.running)return {ok:false,reason:'BOT_NOT_RUNNING'};
  if(s.pause_requested)return {ok:false,reason:'PAUSE_PENDING'};
  if(s.halted)return {ok:false,reason:'EMERGENCY_STOP'};
  if(lossHit(s))return {ok:false,reason:'DAILY_LOSS_LIMIT'};
  if(!SYMBOLS.includes(sym))return {ok:false,reason:'UNKNOWN_SYMBOL'};
  if(sym in s.positions)return {ok:false,reason:'DUPLICATE_POSITION'};
  if(Object.keys(s.positions).length>=s.limits.max_positions)return {ok:false,reason:'POSITION_LIMIT'};
  const price=s.prices[sym],stop=s.limits.stop_loss_pct/100,risk=s.initial*s.limits.risk_per_trade_pct/100;
  if(!valid(price)||price<=0||!valid(stop)||stop<=0)return {ok:false,reason:'INVALID_PRICE'};
  const reserved=Object.values(s.positions).reduce((sum,p)=>sum+p.entry*p.qty,0);
  const available=Math.max(0,Math.min(s.cash,s.initial-reserved));
  const q=Math.floor(Math.min(risk/(price*stop),s.initial*s.limits.max_allocation_pct/100/price,available/price)*10000)/10000;
  if(!valid(q)||q<=0||q*price>available+1e-7)return {ok:false,reason:'CAPITAL_LIMIT'};
  return {ok:true,qty:q,price};
}
function buy(s,sym){
  const decision=checkEntry(s,sym);
  if(!decision.ok){
    // Record only strategy-signal rejections, bounded by existing 100-event log.
    log(s,'risk','REJECT '+sym+': '+decision.reason);
    return false;
  }
  const {qty:q,price}=decision;
  const stop=s.limits.stop_loss_pct/100;
  s.cash=round(s.cash-q*price);
  s.positions[sym]={qty:q,entry:round(price,5),stop:round(price*(1-stop)),target:round(price*(1+s.limits.take_profit_pct/100)),opened_tick:s.tick,opened_at:nowIso(),policy_version:'0.2.1'};
  log(s,'buy',`BUY ${sym} ${q.toFixed(4)} @ ${price.toFixed(2)} (synthetic)`);return true;
}
export function step(s){
  applyPolicy(s);
  const today=new Date().toISOString().slice(0,10);
  if(today!==s.day){s.day=today;s.day_equity=equity(s);log(s,'info','New UTC day: paper daily baseline reset')}
  s.tick++;
  SYMBOLS.forEach((sym,i)=>{
    const old=s.prices[sym],wave=.0008*Math.sin(s.tick/7+i*2),change=Math.max(-.015,Math.min(.015,normal(s)*.003+wave));
    s.prices[sym]=round(Math.max(.01,old*(1+change)));s.history[sym].push(s.prices[sym]);s.history[sym]=s.history[sym].slice(-80);
  });
  for(const [sym,p] of Object.entries(s.positions)){
    if(s.prices[sym]<=p.stop)close(s,sym,'stop-loss (tick-based)');
    else if(s.prices[sym]>=p.target)close(s,sym,'take-profit');
    else if((p.opened_at && Date.now()-Date.parse(p.opened_at)>=POLICY.max_hold_minutes*60000) || (!p.opened_at && s.tick-p.opened_tick>=POLICY.max_hold_minutes))close(s,sym,'time exit (15 min)');
  }
  if(lossHit(s)&&!s.halted){s.running=false;s.halted=true;s.pause_requested=false;log(s,'risk','Daily loss limit triggered: no new entries; current positions still monitored')}
  if(s.pause_requested&&Object.keys(s.positions).length===0){s.pause_requested=false;s.running=false;log(s,'info','All open trades finished; PAUZA')}
  if(s.running&&!s.halted&&!s.pause_requested){
    for(const sym of SYMBOLS){const h=s.history[sym],avg=h.slice(-12).reduce((a,b)=>a+b,0)/12;
      if(h.at(-1)>avg*1.002&&h.at(-1)>h.at(-4))buy(s,sym);
    }
  }
  // Keep timestamped snapshots for 7-day chart windows; do not invent timestamps for earlier snapshots.
  if(s.tick%2===0){s.equity_history.push({tick:s.tick,at:nowIso(),equity:round(equity(s),4)});s.equity_history=s.equity_history.slice(-6000)}
  assertSafe(s);return s;
}
export function act(s,action){
  applyPolicy(s);
  if(action==='start'){
    if(s.halted||lossHit(s))return {ok:false,message:'Obchodovanie je zablokované denným limitom alebo STOP.'};
    s.running=true;s.pause_requested=false;log(s,'info','Synthetic paper trading started');
  }else if(action==='pause'){
    s.running=false;s.pause_requested=Object.keys(s.positions).length>0;
    log(s,'info',s.pause_requested?'ČAKÁ NA UZAVRETIE: no new entries':'PAUZA: no open positions');
  }else if(action==='stop'){
    s.running=false;s.pause_requested=false;s.halted=true;
    for(const sym of Object.keys(s.positions))close(s,sym,'emergency close');
    log(s,'risk','Emergency STOP: all synthetic positions closed');
  }else if(action==='reset'){
    if(s.running||s.pause_requested||Object.keys(s.positions).length)return {ok:false,message:'Pred resetom musíš ukončiť všetky pozície.'};
    const n=fresh(s.initial);Object.keys(s).forEach(k=>delete s[k]);Object.assign(s,n);log(s,'info','Paper account reset');
  }else return {ok:false,message:'Unknown action'};
  assertSafe(s);return {ok:true,message:'OK'};
}
export function assertSafe(s){
  if(!valid(s.initial)||s.initial<=0||!valid(s.cash)||s.cash < -0.00001 || !valid(equity(s)))throw Error('Capital safety invariant failed');
  const committed=Object.values(s.positions).reduce((a,p)=>a+p.entry*p.qty,0);
  if(committed>s.initial+0.01)throw Error('Committed capital exceeded initial capital');
  if(Object.keys(s.positions).length>s.limits.max_positions)throw Error('Position count exceeded');
  for(const [sym,p] of Object.entries(s.positions))if(!SYMBOLS.includes(sym)||!valid(p.qty)||p.qty<=0||!valid(p.entry)||p.entry<=0||!valid(s.prices[sym])||s.prices[sym]<=0)throw Error('Invalid position');
}
export function view(s){
  const eq=equity(s),inv=invested(s),wins=s.trades.filter(t=>t.pnl>0).length,losses=s.trades.filter(t=>t.pnl<0).length;
  const grossProfit=s.trades.reduce((v,t)=>v+Math.max(0,t.pnl),0),grossLoss=s.trades.reduce((v,t)=>v+Math.max(0,-t.pnl),0);
  const closedPnl=s.trades.reduce((v,t)=>v+t.pnl,0);
  const series=[s.initial,...s.equity_history.map(x=>x.equity),eq];let peak=series[0],drawdownPct=0;
  for(const val of series){peak=Math.max(peak,val);if(peak>0)drawdownPct=Math.max(drawdownPct,(peak-val)/peak*100)}
  return {version:VERSION,mode:'SYNTHETIC PAPER · NO LIVE DATA',running:s.running,pause_requested:s.pause_requested,halted:s.halted,
    status:s.halted?'STOP':s.pause_requested?'ČAKÁ NA UZAVRETIE':s.running?'BEŽÍ':'PAUZA',tick:s.tick,
    initial:s.initial,cash:round(s.cash,2),equity:round(eq,2),invested:round(inv,2),pnl:round(eq-s.initial,2),
    day_equity:s.day_equity,daily_loss_hit:lossHit(s),daily_loss_remaining:round(Math.max(0,eq-s.day_equity*(1-s.limits.daily_loss_pct/100)),2),committed:round(Object.values(s.positions).reduce((a,p)=>a+p.entry*p.qty,0),2),limits:s.limits,prices:s.prices,
    positions:Object.entries(s.positions).map(([symbol,p])=>({symbol,...p,market:s.prices[symbol],unrealized:round(p.qty*(s.prices[symbol]-p.entry),4),remaining_seconds:Math.max(0,Math.ceil((s.limits.max_hold_minutes*60000-(p.opened_at?Date.now()-Date.parse(p.opened_at):(s.tick-p.opened_tick)*60000))/1000))})),
    trades:s.trades.slice(0,50),trade_count:s.trades.length,wins,losses,stats:{win_rate:s.trades.length?round(wins/s.trades.length*100,2):null,profit_factor:grossLoss?round(grossProfit/grossLoss,2):(grossProfit>0?null:0),gross_profit:round(grossProfit,2),gross_loss:round(grossLoss,2),closed_pnl:round(closedPnl,2),max_drawdown_pct:round(drawdownPct,2)},equity_history:s.equity_history.slice(-6000),events:s.events.slice(0,35)};
}
