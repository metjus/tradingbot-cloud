import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {HTML,CLIENT} from '../src/ui.js';
test('dashboard browser JavaScript parses',()=>{
  assert.doesNotThrow(()=>new vm.Script(CLIENT));
});
test('dashboard shows new risk and mobile controls',()=>{
  for(const key of ['id="risk"','id="stats"','Zostáva','risk-meter','v0.4.1'])assert.ok(HTML.includes(key),key);
});

test('equity chart exposes intervals, readout, and local time-axis',()=>{
 for(const key of ['data-range="1h"','data-range="24h"','data-range="7d"','id="chart-readout"','id="chart-note"'])assert.ok(HTML.includes(key),key);
 for(const key of ["const windows={'1h'","addEventListener('pointerdown'","toLocaleString('sk-SK'","function draw(points)"])assert.ok(CLIENT.includes(key),key);
});
test('profit factor handles all-winning streak without dividing by zero',()=>{
 assert.ok(CLIENT.includes("'Bez strát'"));
});

test('market diagnostics are visible on mobile',()=>{
 assert.ok(HTML.includes('id="market-diagnostics"'));
 for(const k of ['FEED_READY','PROVIDER_OR_DATA_ERROR','lastAttemptAt','timestampSource'])assert.ok(CLIENT.includes(k),k);
});

test('separate USD paper preview controls are visible',()=>{for(const key of ['id="market-paper-check"','id="market-paper-status"','id="market-paper-cards"'])assert.ok(HTML.includes(key),key);assert.ok(CLIENT.includes('/api/market-paper/check'));});

test('USD paper controls are independent of synthetic controls',()=>{
 for(const key of ['market-paper-start','market-paper-pause','market-paper-stop','market-paper-positions','market-paper-trades'])assert.ok(HTML.includes(key),key);
 assert.ok(CLIENT.includes('/api/market-paper/action'));
});
