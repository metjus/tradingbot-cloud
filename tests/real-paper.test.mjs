import test from 'node:test';
import assert from 'node:assert/strict';
import {newRealPaper,simulateRealStep,quoteGate,previewPaper,validatePaper} from '../src/real-paper.js';
const now=new Date('2026-11-05T15:00:00Z');
function feed(when=now){
 return {sessionOpen:true,readyForPaper:true,diagnostic:'FEED_READY',updatedAt:when.toISOString(),
 quotes:['AAPL','MSFT','NVDA'].map((symbol,i)=>({symbol,price:100+i*50,currency:'USD',timestamp:Math.floor(when.getTime()/1000),timestampSource:'last_quote_at',fresh:true}))};
}
test('new market paper sandbox is USD and isolated at 500 dollars',()=>{
 const s=newRealPaper();assert.equal(s.cash,500);assert.equal(s.currency,'USD');assert.equal(Object.keys(s.positions).length,0);
 assert.equal(previewPaper(s,feed(),now).canCheck,true);
});
test('fresh approved data can be inspected once without initiating trades',()=>{
 const s=newRealPaper();const m=feed();
 const r=simulateRealStep(s,m,now);assert.equal(r.ok,true);
 assert.deepEqual(r.signals.map(x=>x.action),['HOLD','HOLD','HOLD']);
 assert.equal(Object.keys(s.positions).length,0);assert.equal(s.cash,500);
 assert.equal(simulateRealStep(s,m,now).reason,'DUPLICATE_SNAPSHOT');
});
test('stale, incomplete, mismatched and closed data fail closed',()=>{
 const m=feed();const s=newRealPaper();
 assert.equal(quoteGate({...m,sessionOpen:false},now).reason,'MARKET_CLOSED');
 assert.equal(quoteGate({...m,quotes:m.quotes.slice(0,2)},now).reason,'INVALID_NVDA');
 assert.equal(quoteGate({...m,diagnostic:'STALE_OR_UNVERIFIED'},now).reason,'FEED_NOT_READY');
 assert.equal(quoteGate(feed(new Date('2026-11-05T14:45:00Z')),now).reason,'SNAPSHOT_TOO_OLD');
 assert.equal(Object.keys(s.positions).length,0);
});
test('market paper safety rejects overcommitted positions',()=>{
 const s=newRealPaper();s.positions.AAPL={qty:10,cost:1000};
 assert.throws(()=>validatePaper(s),/PAPER_CAPITAL_INVALID/);
});
