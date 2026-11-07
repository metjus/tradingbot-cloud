import test from 'node:test';
import assert from 'node:assert/strict';
import {validateDemoOrder,selectDemoInstruments,demoRequest,demoOverview,DEMO_LIMITS} from '../src/demo-orders.js';
const catalog=[{ticker:'AAPL_US_EQ',name:'Apple',type:'STOCK',currencyCode:'USD'},
 {ticker:'CSPX_L_EQ',name:'S&P 500 ETF',type:'ETF',currencyCode:'EUR'}];
const account={currency:'USD',availableToTrade:500};
const base={ticker:'AAPL_US_EQ',side:'BUY',quantity:.1,limitPrice:200};
test('Demo limit order risk allows a small verified same-currency stock',()=>{
 const r=validateDemoOrder(base,{catalog,account});
 assert.equal(r.ok,true);assert.equal(r.notional,20);
 assert.equal(r.currency,'USD');
 assert.equal(DEMO_LIMITS.totalBuyBudget,100);
});
test('Demo order rejects currency mismatch, unauthenticated instruments and oversized buys',()=>{
 assert.equal(validateDemoOrder({...base,ticker:'CSPX_L_EQ'},{catalog,account}).reason,'ACCOUNT_CURRENCY_MISMATCH');
 assert.equal(validateDemoOrder({...base,ticker:'GUESSED_US_EQ'},{catalog,account}).reason,'TICKER_NOT_IN_VERIFIED_CATALOG');
 assert.equal(validateDemoOrder({...base,quantity:1},{catalog,account}).reason,'ORDER_CAP_25');
 assert.equal(validateDemoOrder(base,{catalog,account,priorBought:85}).reason,'LIFETIME_TEST_BUDGET_100');
 assert.equal(validateDemoOrder(base,{catalog,account:{...account,availableToTrade:10}}).reason,'INSUFFICIENT_DEMO_CASH');
});
test('Demo SELL requires tracked buys and broker available position, never sell arbitrary holdings',()=>{
 const input={...base,side:'SELL'};
 assert.equal(validateDemoOrder(input,{catalog,account,availableToSell:0}).reason,'SELL_EXCEEDS_TRACKED_BUYS');
 assert.equal(validateDemoOrder(input,{catalog,account,availableToSell:.1,positions:[{instrument:{ticker:'AAPL_US_EQ'},quantityAvailableForTrading:.05}]}).reason,'SELL_EXCEEDS_BROKER_POSITION');
 assert.equal(validateDemoOrder(input,{catalog,account,availableToSell:.1,positions:[{instrument:{ticker:'AAPL_US_EQ'},quantityAvailableForTrading:.1}]}).ok,true);
});
test('Demo instruments curated by exact platform ticker and eligible stock/ETF names',()=>{
 const items=selectDemoInstruments([
  ...catalog,{ticker:'EURUSD_FOREX',name:'EUR/USD',type:'FOREX',currencyCode:'USD'},
  {ticker:'BTC_EQ',name:'Bitcoin',type:'CRYPTO',currencyCode:'USD'}
 ]);
 assert.equal(items.length,2);
 assert.deepEqual(items.map(x=>x.ticker),['AAPL_US_EQ','CSPX_L_EQ']);
});
test('Demo API refuses credentials missing and permits only the DEMO host',async()=>{
 await assert.rejects(demoRequest({},'/equity/orders',{},async()=>{throw Error('should never call')}),/DEMO_KEYS_MISSING/);
 const calls=[];
 const fetcher=async(url,opts)=>{calls.push({url,method:opts.method});return {ok:true,json:async()=>[]} };
 const data=await demoRequest({T212_DEMO_API_KEY:'key',T212_DEMO_API_SECRET:'secret'},'/equity/orders',{},fetcher);
 assert.deepEqual(data,[]);assert.equal(calls.length,1);
 assert.equal(calls[0].url,'https://demo.trading212.com/api/v0/equity/orders');
 assert.equal(calls[0].method,'GET');
});
test('Broker account precheck reads summary positions and pending orders',async()=>{
 const fetcher=async(url)=>({ok:true,json:async()=>url.endsWith('/summary')?{currency:'EUR',cash:{availableToTrade:300},totalValue:500}:[]});
 const result=await demoOverview({T212_DEMO_API_KEY:'key',T212_DEMO_API_SECRET:'secret'},fetcher);
 assert.equal(result.account.currency,'EUR');
 assert.deepEqual(result.positions,[]);assert.deepEqual(result.pending,[]);
});
