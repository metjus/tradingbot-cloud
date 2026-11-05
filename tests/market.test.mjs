import test from 'node:test';
import assert from 'node:assert/strict';
import {WATCH_SYMBOLS,nySession,normalizeQuote,quoteIsFresh} from '../src/market.js';

test('market watch uses only explicit USD watchlist and rejects malformed quotes',()=>{
 assert.deepEqual(WATCH_SYMBOLS,['AAPL','MSFT','NVDA']);
 const time=Math.floor(Date.parse('2026-11-04T15:00:00Z')/1000);
 const q=normalizeQuote('AAPL',{symbol:'AAPL',close:'183.24',currency:'USD',timestamp:time-3600,last_quote_at:time});
 assert.equal(q.price,183.24);
 assert.equal(q.timestampSource,'last_quote_at');
 const barOnly=normalizeQuote('AAPL',{symbol:'AAPL',close:'183.24',currency:'USD',timestamp:time});
 assert.equal(quoteIsFresh(barOnly,time*1000),false);
 assert.ok(quoteIsFresh(q,time*1000+60000));
 assert.equal(quoteIsFresh(q,time*1000+21*60000),false);
 assert.throws(()=>normalizeQuote('TSLA',{symbol:'TSLA',close:10,timestamp:time}));
 assert.throws(()=>normalizeQuote('AAPL',{symbol:'MSFT',close:10,timestamp:time}));
 assert.throws(()=>normalizeQuote('AAPL',{symbol:'AAPL',close:0,timestamp:time}));
 assert.throws(()=>normalizeQuote('AAPL',{symbol:'AAPL',close:10,timestamp:0}));
 assert.throws(()=>normalizeQuote('AAPL',{symbol:'AAPL',close:10,currency:'EUR',timestamp:time}));
});
test('regular New York session excludes evening and weekends',()=>{
 assert.equal(nySession(new Date('2026-11-04T15:00:00Z')),true);
 assert.equal(nySession(new Date('2026-11-04T22:00:00Z')),false);
 assert.equal(nySession(new Date('2026-11-07T15:00:00Z')),false);
});

test('outside-session feed is explicitly unavailable',async()=>{
 const {loadMarket,refreshMarket}=await import('../src/market.js');
 const sample=await loadMarket({}, {TWELVEDATA_API_KEY:'test'},new Date('2026-11-07T15:00:00Z')).catch(()=>null);
 assert.ok(sample===null || sample.sessionOpen===false);
 assert.deepEqual(await refreshMarket({}, {TWELVEDATA_API_KEY:'test'},new Date('2026-11-07T15:00:00Z')), {skipped:true,reason:'MARKET_CLOSED'});
});
test('unconfigured market status never claims readiness',async()=>{
 const {loadMarket}=await import('../src/market.js');
 const status=await loadMarket({}, {},new Date('2026-11-05T15:00:00Z'));
 assert.equal(status.configured,false);
 assert.equal(status.readyForPaper,undefined);
});
