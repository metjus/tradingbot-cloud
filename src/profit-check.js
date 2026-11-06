// Paper-trading profit feasibility checks; no profitability guarantee or broker orders.
// Models spread/slippage on both legs and requires a measurable target after costs.
export const MODEL={spreadBps:10,slippageBps:5,feeUsd:0};
const num=x=>typeof x==='number'&&Number.isFinite(x);
const fix=x=>Math.round(x*1e6)/1e6;
const buy=(p,m)=>p*(1+(m.spreadBps/2+m.slippageBps)/10000);
const sell=(p,m)=>p*(1-(m.spreadBps/2+m.slippageBps)/10000);
export function checkEntryProfit({price,quantity,targetPct,stopPct,model=MODEL}){
 if(![price,quantity,targetPct,stopPct,model.spreadBps,model.slippageBps,model.feeUsd].every(num)||
 price<=0||quantity<=0||targetPct<=0||stopPct<=0||model.spreadBps<0||model.slippageBps<0||model.feeUsd<0)
  return {ok:false,reason:'INVALID_INPUT'};
 const entryCost=quantity*buy(price,model)+model.feeUsd;
 const win=quantity*sell(price*(1+targetPct/100),model)-model.feeUsd-entryCost;
 const lose=quantity*sell(price*(1-stopPct/100),model)-model.feeUsd-entryCost;
 if(win<=0)return {ok:false,reason:'TARGET_NOT_PROFITABLE_AFTER_COSTS',netTargetUsd:fix(win),netStopUsd:fix(lose)};
 if(lose>=0)return {ok:false,reason:'INVALID_STOP_MODEL'};
 const requiredWinRatePct=100*(-lose)/(win-lose);
 return {ok:true,reason:'PAYOFF_FEASIBLE_NOT_PROVEN',netTargetUsd:fix(win),netStopUsd:fix(lose),
  rewardRiskRatio:fix(win/(-lose)),breakEvenWinRatePct:fix(requiredWinRatePct),
  // This does NOT imply a positive expectation. Empirical win probability is not available.
  expectedNetProfitUsd:null,validatedEdge:false,liveAllowed:false};
}
export function checkExitProfit({cost,quantity,price,reason,model=MODEL}){
 if(![cost,quantity,price,model.spreadBps,model.slippageBps,model.feeUsd].every(num)||
 cost<=0||quantity<=0||price<=0||model.spreadBps<0||model.slippageBps<0||model.feeUsd<0)
  return {ok:false,reason:'INVALID_INPUT'};
 const netPnlUsd=fix(quantity*sell(price,model)-model.feeUsd-cost);
 // Safety exits must execute even when net P/L is negative; never refuse a stop-loss
 // just because a position would realize a loss.
 const protective=['STOP_LOSS','TIME_EXIT','EMERGENCY_EXIT','DAILY_LOSS_LIMIT'].includes(reason);
 if(!protective&&netPnlUsd<=0)return {ok:false,reason:'DISCRETIONARY_EXIT_NOT_PROFITABLE',netPnlUsd};
 return {ok:true,reason:protective?'PROTECTIVE_EXIT':'PROFITABLE_EXIT',netPnlUsd,netPositive:netPnlUsd>0};
}
