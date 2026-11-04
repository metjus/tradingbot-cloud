import test from 'node:test';
import assert from 'node:assert/strict';
import {fresh,step,act,view,equity,assertSafe} from '../src/engine.js';
test('capital bound and position count over 2000 synthetic ticks',()=>{
 const s=fresh(500);assert.equal(act(s,'start').ok,true);
 for(let i=0;i<2000;i++){step(s);assertSafe(s);assert.ok(s.cash>=0);assert.ok(Object.keys(s.positions).length<=2);
   const cost=Object.values(s.positions).reduce((a,p)=>a+p.entry*p.qty,0);
   assert.ok(cost<=s.initial*.5+0.01); // 2 positions, each up to 25% of initial budget
 }
});
test('pause drains positions and cannot open new positions',()=>{
 const s=fresh();act(s,'start');for(let i=0;i<1000&&!Object.keys(s.positions).length;i++)step(s);
 assert.ok(Object.keys(s.positions).length>0);assert.equal(act(s,'pause').ok,true);assert.equal(view(s).status,'ČAKÁ NA UZAVRETIE');
 const existing=new Set(Object.keys(s.positions));for(let i=0;i<3000&&s.pause_requested;i++){
   step(s);for(const sym of Object.keys(s.positions))assert.ok(existing.has(sym));
 }
 assert.equal(view(s).status,'PAUZA');assert.equal(Object.keys(s.positions).length,0);
});
test('emergency stop flattens and blocks restart, reset re-enables',()=>{
 const s=fresh();act(s,'start');for(let i=0;i<1000&&!Object.keys(s.positions).length;i++)step(s);
 assert.equal(act(s,'stop').ok,true);assert.equal(Object.keys(s.positions).length,0);assert.equal(s.halted,true);
 assert.equal(act(s,'start').ok,false);assert.equal(act(s,'reset').ok,true);assert.equal(s.halted,false);assert.equal(s.cash,500);
});
test('reset forbidden while trading or with open positions',()=>{
 const s=fresh();act(s,'start');assert.equal(act(s,'reset').ok,false);
});
