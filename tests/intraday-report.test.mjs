import test from 'node:test';
import assert from 'node:assert/strict';
import {intradayReport} from '../src/intraday-report.js';
test('empty research sample does not claim profitability',()=>{
 const r=intradayReport();
 assert.equal(r.sampleSize,0);assert.equal(r.winRatePct,null);assert.equal(r.profitFactor,null);
 assert.equal(r.verdict,'NOT_VALIDATED');assert.equal(r.liveApproved,false);
 assert.equal(r.sampleRemaining,100);
});
test('paper report breaks down net P/L, winrate and hold duration by symbol',()=>{
 const trades=[
  {symbol:'AAPL',pnl:2,openedAt:'2026-11-05T15:00:00Z',closedAt:'2026-11-05T15:15:00Z'},
  {symbol:'MSFT',pnl:-1,openedAt:'2026-11-05T16:00:00Z',closedAt:'2026-11-05T16:05:00Z'},
  {symbol:'AAPL',pnl:1,openedAt:'2026-11-05T17:00:00Z',closedAt:'2026-11-05T17:10:00Z'}
 ];
 const r=intradayReport(trades,[],500);
 assert.equal(r.sampleSize,3);assert.equal(r.netPnlUsd,2);assert.equal(r.profitFactor,3);
 assert.equal(r.winRatePct,66.6667);assert.equal(r.averageHoldMinutes,10);
 assert.equal(r.bySymbol[0].trades,2);assert.equal(r.bySymbol[1].trades,1);
 assert.equal(r.liveApproved,false);
});
test('unrecognized instruments and malformed trades do not corrupt statistics',()=>{
 const r=intradayReport([{symbol:'EURUSD',pnl:999},{symbol:'AAPL',pnl:NaN},{symbol:'NVDA',pnl:-2}],[],500);
 assert.equal(r.sampleSize,1);assert.equal(r.netPnlUsd,-2);assert.equal(r.maxConsecutiveLosses,1);
});
