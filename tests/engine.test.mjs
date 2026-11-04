import test from 'node:test';
import assert from 'node:assert/strict';
import {fresh,step,act,view,equity,assertSafe,checkEntry} from '../src/engine.js';
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

test('new paper risk settings and 15-minute time exit',()=>{
 const s=fresh();
 assert.equal(s.limits.stop_loss_pct,0.8);
 assert.equal(s.limits.take_profit_pct,0.6);
 assert.equal(s.limits.max_hold_minutes,15);
 s.positions['SIM-ALPHA']={qty:1,entry:100,stop:99.2,target:100.6,opened_tick:0,opened_at:new Date(Date.now()-16*60000).toISOString(),policy_version:'0.2.1'};
 s.cash=400;
 // Even if TP/SL is not reached, an expired position must close.
 step(s);
 assert.equal(Object.keys(s.positions).length,0);
 assert.equal(s.trades[0].reason,'time exit (15 min)');
});
test('existing paper account migrates to v0.2.1 rules without reset',()=>{
 const s=fresh();
 s.limits.stop_loss_pct=2;s.limits.take_profit_pct=1.2;
 s.positions['SIM-ALPHA']={qty:1,entry:100,stop:98,target:101.2,opened_tick:0};
 s.cash=400;
 act(s,'pause');
 assert.equal(s.positions['SIM-ALPHA'].stop,99.2);
 assert.equal(s.positions['SIM-ALPHA'].target,100.6);
 assert.equal(s.limits.max_hold_minutes,15);
});

test('risk manager rejects duplicate symbol and capped positions',()=>{
 const s=fresh();act(s,'start');
 s.positions['SIM-ALPHA']={entry:100,qty:1,stop:99.2,target:100.6,opened_tick:1};
 s.cash=400;
 assert.equal(checkEntry(s,'SIM-ALPHA').reason,'DUPLICATE_POSITION');
 s.positions['SIM-BETA']={entry:55,qty:1,stop:54.56,target:55.33,opened_tick:1};
 s.cash=345;
 assert.equal(checkEntry(s,'SIM-GAMMA').reason,'POSITION_LIMIT');
 act(s,'pause');assert.equal(checkEntry(s,'SIM-GAMMA').reason,'BOT_NOT_RUNNING');
});
test('risk manager enforces total initial allocation and daily drawdown',()=>{
 const s=fresh();act(s,'start');
 s.positions['SIM-ALPHA']={entry:100,qty:4.99,stop:99.2,target:100.6,opened_tick:1};
 s.cash=1;
 assert.equal(checkEntry(s,'SIM-BETA').reason,'CAPITAL_LIMIT');
 s.positions={};s.cash=490;s.day_equity=500;
 assert.equal(checkEntry(s,'SIM-ALPHA').reason,'DAILY_LOSS_LIMIT');
});
test('risk manager refuses corrupted capital state',()=>{
 const s=fresh();s.positions['SIM-ALPHA']={entry:100,qty:6,stop:99.2,target:100.6,opened_tick:1};
 s.cash=0;assert.throws(()=>assertSafe(s),/Committed capital/);
});
test('risk manager reports countdown and cash allocation to dashboard',()=>{
 const s=fresh();s.positions['SIM-ALPHA']={entry:100,qty:1,stop:99.2,target:100.6,opened_tick:1,opened_at:new Date(Date.now()-60000).toISOString()};
 s.cash=400;const v=view(s);
 assert.equal(v.committed,100);
 assert.ok(v.positions[0].remaining_seconds<=14*60+2);
 assert.ok(v.positions[0].remaining_seconds>0);
});
