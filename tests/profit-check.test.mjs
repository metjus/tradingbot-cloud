import test from 'node:test';
import assert from 'node:assert/strict';
import {checkEntryProfit,checkExitProfit} from '../src/profit-check.js';

test('entry payoff reports net costs and break-even hit rate without invented predicted edge',()=>{
 const r=checkEntryProfit({price:100,quantity:1,targetPct:.6,stopPct:.8});
 assert.equal(r.ok,true);
 assert.ok(r.netTargetUsd>0&&r.netStopUsd<0);
 assert.ok(r.breakEvenWinRatePct>70);
 assert.equal(r.validatedEdge,false);
 assert.equal(r.liveAllowed,false);
 assert.equal(r.expectedNetProfitUsd,null);
});
test('entry rejected if fees and spread destroy its entire profit target',()=>{
 const r=checkEntryProfit({price:100,quantity:1,targetPct:.05,stopPct:.8});
 assert.equal(r.ok,false);
 assert.equal(r.reason,'TARGET_NOT_PROFITABLE_AFTER_COSTS');
});
test('profit-taking requires positive net P/L, unlike safety exits',()=>{
 const unprofitable={cost:101,quantity:1,price:100,reason:'TAKE_PROFIT'};
 assert.equal(checkExitProfit(unprofitable).ok,false);
 const stop=checkExitProfit({...unprofitable,reason:'STOP_LOSS'});
 assert.equal(stop.ok,true);assert.ok(stop.netPnlUsd<0);
 assert.equal(checkExitProfit({...unprofitable,reason:'EMERGENCY_EXIT'}).ok,true);
 assert.equal(checkExitProfit({...unprofitable,reason:'TIME_EXIT'}).ok,true);
});
test('reject invalid inputs fail closed',()=>{
 for(const price of [0,-1,NaN,Infinity]){
  assert.equal(checkEntryProfit({price,quantity:1,targetPct:.6,stopPct:.8}).ok,false);
  assert.equal(checkExitProfit({price,cost:100,quantity:1,reason:'STOP_LOSS'}).ok,false);
 }
});
