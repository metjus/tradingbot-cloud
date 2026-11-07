// Intraday diagnostics for isolated market-price paper trades.
// Metrics are descriptive only: they never certify profitability or enable live orders.
const finite=n=>typeof n==='number'&&Number.isFinite(n);
const round=n=>Math.round(n*1e4)/1e4;
export const RESEARCH_POLICY=Object.freeze({targetSample:100,preferredMinHoldMinutes:5,maxHoldMinutes:30,allowed:['AAPL','MSFT','NVDA']});
export function intradayReport(trades=[],positions=[],initial=500){
 const valid=trades.filter(t=>finite(t.pnl)&&RESEARCH_POLICY.allowed.includes(t.symbol));
 const wins=valid.filter(t=>t.pnl>0),losses=valid.filter(t=>t.pnl<0);
 const grossWin=wins.reduce((s,t)=>s+t.pnl,0),grossLoss=-losses.reduce((s,t)=>s+t.pnl,0);
 const net=valid.reduce((s,t)=>s+t.pnl,0);
 const times=valid.map(t=>[Date.parse(t.openedAt||''),Date.parse(t.closedAt||'')])
  .filter(([a,b])=>finite(a)&&finite(b)&&b>=a)
  .map(([a,b])=>(b-a)/60000);
 const duration=times.length?round(times.reduce((a,b)=>a+b,0)/times.length):null;
 let streak=0,worstStreak=0;
 for(const t of [...valid].reverse()){streak=t.pnl<0?streak+1:0;worstStreak=Math.max(worstStreak,streak)}
 const bySymbol=RESEARCH_POLICY.allowed.map(symbol=>{
  const t=valid.filter(v=>v.symbol===symbol);
  return {symbol,trades:t.length,netPnlUsd:round(t.reduce((sum,x)=>sum+x.pnl,0)),winRatePct:t.length?round(t.filter(x=>x.pnl>0).length/t.length*100):null};
 });
 return {
  mode:'INTRADAY_RESEARCH_ONLY',sampleSize:valid.length,targetSample:RESEARCH_POLICY.targetSample,
  sampleRemaining:Math.max(0,RESEARCH_POLICY.targetSample-valid.length),
  verdict:'NOT_VALIDATED',liveApproved:false,netPnlUsd:round(net),
  returnOnInitialPct:initial>0?round(net/initial*100):null,
  winRatePct:valid.length?round(wins.length/valid.length*100):null,
  grossProfitUsd:round(grossWin),grossLossUsd:round(grossLoss),
  profitFactor:grossLoss>0?round(grossWin/grossLoss):null,
  averageTradeUsd:valid.length?round(net/valid.length):null,
  averageHoldMinutes:duration,maxConsecutiveLosses:worstStreak,
  openPositions:positions.length,bySymbol,
  note:'Recorded paper outcomes only; small samples and historical fitting cannot establish future positive expectancy.'
 };
}
