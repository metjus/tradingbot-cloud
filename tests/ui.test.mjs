import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {HTML,CLIENT} from '../src/ui.js';
test('dashboard browser JavaScript parses',()=>{
  assert.doesNotThrow(()=>new vm.Script(CLIENT));
});
test('dashboard shows new risk and mobile controls',()=>{
  for(const key of ['id="risk"','id="stats"','Zostáva','risk-meter','v0.3.1'])assert.ok(HTML.includes(key),key);
});
