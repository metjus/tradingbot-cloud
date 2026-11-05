import test from 'node:test';
import assert from 'node:assert/strict';
import {configStatus,readDemo} from '../src/trading212.js';
test('no credentials never calls Trading 212',async()=>{
 let called=false;
 const r=await readDemo({},async()=>{called=true;throw Error('must not fetch')});
 assert.equal(r.connected,false);assert.equal(called,false);
});
test('read-only demo path and basic auth always use GET',async()=>{
 const calls=[];
 const mock=async(url,options)=>{calls.push({url,method:options.method,auth:options.headers.Authorization});
 return {ok:true,json:async()=>url.endsWith('/positions')?[]:{currency:'EUR',totalValue:500,cash:{availableToTrade:450},investments:{currentValue:50}}};
 };
 const r=await readDemo({T212_DEMO_API_KEY:'key',T212_DEMO_API_SECRET:'secret'},mock);
 assert.equal(r.connected,true);assert.equal(r.account.totalValue,500);
 assert.equal(calls.length,2);
 assert.deepEqual(calls.map(c=>c.url),['https://demo.trading212.com/api/v0/equity/account/summary','https://demo.trading212.com/api/v0/equity/positions']);
 assert.ok(calls.every(c=>c.method==='GET'));
 assert.ok(calls.every(c=>c.auth==='Basic '+btoa('key:secret')));
 assert.ok(calls.every(c=>!c.url.includes('live.trading212.com')));
});
test('demo API rejects malformed responses and shows safe error code',async()=>{
 const mock=async()=>({ok:false,status:401});
 const r=await readDemo({T212_DEMO_API_KEY:'key',T212_DEMO_API_SECRET:'secret'},mock);
 assert.equal(r.connected,false);
 assert.equal(r.account,null);
});
