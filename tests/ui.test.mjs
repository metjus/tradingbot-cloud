import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {HTML,CLIENT} from '../src/ui.js';
test('dashboard browser JavaScript parses',()=>{
  assert.doesNotThrow(()=>new vm.Script(CLIENT));
});
test('dashboard shows new risk and mobile controls',()=>{
  for(const key of ['id="risk"','id="stats"','Zostáva','risk-meter','v0.6.3'])assert.ok(HTML.includes(key),key);
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

test('force update button, version marker, and cache busted client are present',()=>{
 for(const key of ['id="force-update"','id="version-check"','client.js?v=0.6.3'])assert.ok(HTML.includes(key),key);
 for(const key of ['APP_VERSION','checkVersion','force_update','location.replace','cache:\'no-store\''])assert.ok(CLIENT.includes(key),key);
});

test('Trading 212 demo order desk requires explicit manual confirmation and shows broker reconciliation',()=>{
 for(const id of ['demo-catalog-load','demo-ticker','demo-side','demo-qty','demo-limit-price','demo-submit-limit','demo-order-log','demo-broker-overview'])assert.ok(HTML.includes(id),id);
 for(const text of ['/api/t212/demo/catalog','/api/t212/demo/limit','/api/t212/demo/overview','confirm(','crypto.randomUUID()'])assert.ok(CLIENT.includes(text),text);
});

test('Slovak decimal comma accepted for iPhone demo quantity and price',()=>{
 assert.ok(HTML.includes('id="demo-qty" type="text" inputmode="decimal"'));
 assert.ok(HTML.includes('id="demo-limit-price" type="text" inputmode="decimal"'));
 assert.ok(CLIENT.includes("function parseDemoDecimal(raw)"));
 assert.ok(CLIENT.includes("quantity=parseDemoDecimal($('demo-qty').value)"));
 assert.ok(CLIENT.includes("limitPrice=parseDemoDecimal($('demo-limit-price').value)"));
 const parser=CLIENT.match(/function parseDemoDecimal\(raw\)\{[\s\S]*?\n\}/)?.[0];
 assert.ok(parser);
 const fn=new Function(parser+'; return parseDemoDecimal;')();
 assert.equal(fn('0,1'),0.1);
 assert.equal(fn('0.1'),0.1);
 assert.equal(fn('12,50'),12.5);
 assert.equal(fn('02'),2);
 assert.ok(Number.isNaN(fn('1,2,3')));
 assert.ok(Number.isNaN(fn('abc')));
 assert.ok(Number.isNaN(fn('')));
});

test('Trading 212 Demo panel is primary and legacy simulators are archived',()=>{
 const demo=HTML.indexOf('Trading 212 · Demo účet');
 const legacy=HTML.indexOf('<details id="legacy-archive"');
 assert.ok(demo>=0&&legacy>demo);
 assert.ok(HTML.includes('TRADING 212 · DEMO ONLY'));
 assert.ok(HTML.includes('SIM a USD paper účty sú archivované'));
 assert.ok(HTML.includes('<summary'));
 assert.ok(HTML.includes('id="demo-submit-limit"'));
});

test('Demo order form reports exact reason for ticker/currency rejection',()=>{
 for(const reason of ['INVALID_TICKER_FORMAT','TICKER_NOT_IN_VERIFIED_CATALOG','ACCOUNT_CURRENCY_MISMATCH','INVALID_SIZE_OR_PRICE'])assert.ok(CLIENT.includes(reason),reason);
 assert.ok(CLIENT.includes('demoOrderErrorMessage(result.reason)'));
});
