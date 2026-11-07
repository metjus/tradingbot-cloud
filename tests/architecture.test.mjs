import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {VERSION as syntheticVersion} from '../src/engine.js';
import {HTML,CLIENT} from '../src/ui.js';
const worker=readFileSync(new URL('../src/worker.js',import.meta.url),'utf8');
const packageJson=JSON.parse(readFileSync(new URL('../package.json',import.meta.url),'utf8'));
test('UI, synthetic engine, worker health and package all expose the same release',()=>{
 const version=packageJson.version;
 assert.equal(version,'0.5.0');
 assert.equal(syntheticVersion,version+'-cloud');
 assert.ok(HTML.includes('Cloud v'+version));
 assert.ok(CLIENT.includes("APP_VERSION='"+version+"'"));
 assert.ok(HTML.includes('/client.js?v='+version));
 assert.ok(worker.includes(version+'-intraday-research'));
});
test('trading 212 integration remains GET-only and bound to Demo URL',()=>{
 const broker=readFileSync(new URL('../src/trading212.js',import.meta.url),'utf8');
 assert.ok(broker.includes('https://demo.trading212.com/api/v0'));
 assert.ok(!broker.includes('https://live.trading212.com'));
 assert.ok(broker.includes("method:'GET'"));
 assert.ok(!/method:\s*['"](?:POST|DELETE|PUT|PATCH)['"]/.test(broker));
});
test('market-price paper is isolated from synthetic state',()=>{
 assert.ok(worker.includes('real_price_paper'));
 assert.ok(worker.includes('paper_state'));
 assert.ok(worker.includes("'/api/market-paper/action'"));
 assert.ok(worker.includes("'/api/t212/demo'"));
});
