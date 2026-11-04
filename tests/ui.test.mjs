import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {HTML,CLIENT} from '../src/ui.js';
test('dashboard browser JavaScript parses',()=>{
  assert.doesNotThrow(()=>new vm.Script(CLIENT));
});
test('dashboard shows new risk and mobile controls',()=>{
  for(const key of ['id="risk"','id="stats"','Zostáva','risk-meter','v0.3.2'])assert.ok(HTML.includes(key),key);
});

test('equity chart exposes intervals, readout, and local time-axis',()=>{
 for(const key of ['data-range="1h"','data-range="24h"','data-range="7d"','id="chart-readout"','id="chart-note"'])assert.ok(HTML.includes(key),key);
 for(const key of ["const windows={'1h'","addEventListener('pointerdown'","toLocaleString('sk-SK'","function draw(points)"])assert.ok(CLIENT.includes(key),key);
});
test('profit factor handles all-winning streak without dividing by zero',()=>{
 assert.ok(CLIENT.includes("'Bez strát'"));
});
