import test from 'node:test';
import assert from 'node:assert/strict';
import {newRealPaper,simulateRealStep,quoteGate,previewPaper,validatePaper,controlPaper,migratePaper} from '../src/real-paper.js';
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
 assert.throws(()=>validatePaper(s),/POSITION_ALLOCATION_INVALID|PAPER_CAPITAL_INVALID/);
});

test('opt-in start opens virtual momentum trade without exceeding capital',()=>{
 const s=newRealPaper();assert.equal(s.paused,true);assert.equal(controlPaper(s,'start',now).ok,true);
 const base=[100,150,200];
 for(let i=0;i<3;i++){
  const at=new Date(now.getTime()+i*5*60000),m=feed(at);
  m.quotes=m.quotes.map((q,j)=>({...q,price:base[j]*(1+i*.002)}));
  const r=simulateRealStep(s,m,at);assert.equal(r.ok,true);
 }
 assert.ok(Object.keys(s.positions).length>0);
 assert.ok(Object.keys(s.positions).length<=2);
 assert.ok(s.cash>=0);
 for(const p of Object.values(s.positions))assert.ok(p.cost<=125.01);
 assert.equal(s.trades.length,0);
});
test('pause blocks new entries while a virtual position can exit on verified TP',()=>{
 const s=newRealPaper();controlPaper(s,'start',now);
 for(let i=0;i<3;i++){
  const at=new Date(now.getTime()+i*5*60000),m=feed(at);
  m.quotes=m.quotes.map(q=>({...q,price:q.price*(1+i*.002)}));
  simulateRealStep(s,m,at);
 }
 assert.ok(Object.keys(s.positions).length>0);
 controlPaper(s,'pause',new Date(now.getTime()+11*60000));
 assert.equal(s.paused,true);
 const at=new Date(now.getTime()+15*60000),m=feed(at);
 m.quotes=m.quotes.map(q=>({...q,price:q.price*1.04}));
 assert.equal(simulateRealStep(s,m,at).ok,true);
 assert.equal(Object.keys(s.positions).length,0);
 assert.equal(s.pauseRequested,false);
 assert.ok(s.trades.length>0);
});
test('emergency stop locks entries and exits on next verified snapshot, not stale prices',()=>{
 const s=newRealPaper();controlPaper(s,'start',now);
 for(let i=0;i<3;i++){
  const at=new Date(now.getTime()+i*5*60000),m=feed(at);
  m.quotes=m.quotes.map(q=>({...q,price:q.price*(1+i*.002)}));
  simulateRealStep(s,m,at);
 }
 assert.ok(Object.keys(s.positions).length>0);
 controlPaper(s,'stop',new Date(now.getTime()+11*60000));
 assert.equal(s.halted,true);
 assert.ok(Object.keys(s.positions).length>0);
 const at=new Date(now.getTime()+15*60000),m=feed(at);
 assert.equal(simulateRealStep(s,{...m,readyForPaper:false},at).ok,false);
 assert.ok(Object.keys(s.positions).length>0);
 assert.equal(simulateRealStep(s,m,at).ok,true);
 assert.equal(Object.keys(s.positions).length,0);
 assert.ok(s.trades.every(t=>t.reason==='EMERGENCY_EXIT'));
 assert.equal(controlPaper(s,'start',at).ok,false);
});

test('paper sandbox does not open fresh positions within last 20 minutes of session',()=>{
 const t=new Date('2026-11-05T20:30:00Z'),s=newRealPaper();
 controlPaper(s,'start',t);
 for(let i=0;i<3;i++){
  const at=new Date(t.getTime()+i*5*60000),m=feed(at);
  m.quotes=m.quotes.map(q=>({...q,price:q.price*(1+i*.002)}));
  assert.equal(simulateRealStep(s,m,at).ok,true);
 }
 assert.equal(Object.keys(s.positions).length,0);
});

test('existing v0.4.0 USD snapshot migrates without clearing balance or last quote',()=>{
 const s=newRealPaper();s.cash=487.25;s.lastQuotes.AAPL=100;
 delete s.samples;delete s.cooldowns;delete s.pauseRequested;delete s.dayEquity;
 migratePaper(s);validatePaper(s);
 assert.equal(s.cash,487.25);
 assert.equal(s.lastQuotes.AAPL,100);
 assert.deepEqual(s.samples,{});
 assert.deepEqual(s.cooldowns,{});
 assert.equal(s.paused,true);
});

test('replayed provider trade timestamps never create a new momentum point',()=>{
 const s=newRealPaper();controlPaper(s,'start',now);
 const first=feed(now);
 assert.equal(simulateRealStep(s,first,now).ok,true);
 const later=new Date(now.getTime()+5*60000);
 const repeated=feed(later);
 repeated.quotes=repeated.quotes.map(q=>({...q,timestamp:Math.floor(now.getTime()/1000)}));
 assert.equal(simulateRealStep(s,repeated,later).reason,'REPEATED_PROVIDER_QUOTE_AAPL');
 assert.equal((s.samples.AAPL||[]).length,1);
 assert.equal(s.lastSnapshotAt,first.updatedAt);
});
test('overnight and large data gaps cannot be misread as momentum',()=>{
 const s=newRealPaper();controlPaper(s,'start',now);
 for(let i=0;i<2;i++){const t=new Date(now.getTime()+i*5*60000);const m=feed(t);m.quotes=m.quotes.map(q=>({...q,price:q.price*(1+i*.002)}));assert.equal(simulateRealStep(s,m,t).ok,true)}
 const late=new Date(now.getTime()+25*60000),m=feed(late);m.quotes=m.quotes.map(q=>({...q,price:q.price*1.01}));
 const result=simulateRealStep(s,m,late);
 assert.equal(result.ok,true);
 assert.equal(result.signals[0].action,'HOLD');
 assert.equal(result.signals[0].reason,'NEED_3_SAMPLES');
});
