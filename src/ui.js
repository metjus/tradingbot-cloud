export const HTML = `<!doctype html><html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trading Bot Cloud v0.6.1</title><style>
:root{font-family:Inter,system-ui,Arial;background:#0b1120;color:#edf2ff}*{box-sizing:border-box}body{margin:0;padding:24px;max-width:1200px;margin:auto}header,.actions{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}h1{font-size:25px;margin:0}h2{font-size:16px;margin:0 0 15px}.sub,small{color:#9ba9c4}.tag{color:#a7e9d1;background:#173b34;padding:7px 12px;border-radius:30px;font-size:12px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:23px 0}.card,.panel{background:#151f32;border:1px solid #2a3650;border-radius:15px;padding:18px}.value{font-size:26px;font-weight:750;margin-top:9px}.panel{margin-bottom:14px;overflow-x:auto}.actions{justify-content:flex-start;margin:16px 0}button{border:0;border-radius:9px;padding:12px 17px;background:#4d77e8;color:white;font-weight:700;cursor:pointer}button.secondary{background:#33435d}button.danger{background:#9f3543}button:disabled{opacity:.5}.risk-meter{height:9px;border-radius:9px;background:#293950;overflow:hidden;margin-top:9px}.risk-fill{height:100%;background:#64deb1;transition:width .25s}.risk-fill.warn{background:#ff8b97}.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}.stat{border:1px solid #2b3953;padding:11px;border-radius:9px}.stat small{display:block;margin-bottom:5px}.stat strong{font-size:16px}table{width:100%;border-collapse:collapse;font-size:13px;min-width:540px}td,th{text-align:left;padding:11px;border-bottom:1px solid #2b3953}th{color:#9ba9c4}.green{color:#64deb1}.red{color:#ff8b97}.neutral{color:#aebbd2}.pnl-cell{font-weight:750;font-variant-numeric:tabular-nums;white-space:nowrap}#message{min-height:20px;color:#ffbd7c}footer{font-size:12px;color:#9ba9c4;line-height:1.7}#chart{width:100%;height:205px;display:block;touch-action:pan-y;cursor:crosshair}.version-controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.version-controls button{padding:8px 12px;white-space:nowrap}.version-controls small{min-width:110px}.chart-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.chart-head h2{margin:0}.chart-range{display:flex;gap:5px}.chart-range button{padding:7px 12px;background:#293950;color:#b9c7de;border:1px solid #3a4b64;font-size:12px}.chart-range button.active{background:#4d77e8;color:white;border-color:#4d77e8}.chart-readout{color:#b9c7de;min-height:20px;font-size:13px;margin:10px 0 6px;font-variant-numeric:tabular-nums} .pill{padding:5px 10px;border:1px solid #2a3650;border-radius:8px}@media(max-width:560px){body{padding:13px}h1{font-size:20px}.card{padding:13px}.value{font-size:22px}.actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.actions button{min-height:44px;padding:10px 8px;font-size:12px}.actions [data-action=\"stop\"]{grid-column:span 2}table{min-width:0}td,th{padding:9px 7px;white-space:nowrap}.panel{padding:14px}.stat-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><header><div><h1>Trading Bot <span class="sub">Cloud v0.6.1</span></h1><small>Cloudflare Workers · persistent paper trading</small></div><div class="version-controls"><span class="tag">SYNTHETIC PAPER · NO REAL MONEY</span><button class="secondary" id="force-update" type="button" title="Načíta najnovšiu verziu aplikácie">Force Update</button><small id="version-check" aria-live="polite">Overujem verziu…</small></div></header>
<div class="cards"><div class="card"><small>Hodnota účtu</small><div class="value" id="equity">–</div></div><div class="card"><small>Voľná hotovosť</small><div class="value" id="cash">–</div></div><div class="card"><small>Investované v pozíciách</small><div class="value" id="invested">–</div></div><div class="card"><small>Celkový P/L</small><div class="value" id="pnl">–</div></div><div class="card"><small>Stav bota</small><div class="value" id="status">–</div></div></div>
<div class="actions"><button data-action="start">Spustiť</button><button class="secondary" data-action="pause">Pauza po dokončení</button><button class="danger" data-action="stop">Núdzové STOP</button><button class="secondary" data-action="reset">Reset demo účtu</button><button class="secondary" id="logout">Odhlásiť sa</button></div><div id="message" role="status"></div>
<section class="panel"><div class="chart-head"><h2>Vývoj hodnoty účtu</h2><div class="chart-range" role="group" aria-label="Časový rozsah grafu"><button data-range="1h" type="button">1H</button><button data-range="24h" type="button" class="active">24H</button><button data-range="7d" type="button">7D</button></div></div><div id="chart-readout" class="chart-readout" aria-live="polite">Dotkni sa grafu pre hodnotu a čas.</div><svg id="chart" viewBox="0 0 440 205" preserveAspectRatio="none" role="img" aria-label="Vývoj hodnoty virtuálneho účtu v čase"><text x="40" y="100" fill="#9ba9c4">Načítavam graf…</text></svg><small id="chart-note">Graf využíva nové časové záznamy a neovplyvňuje obchodovanie.</small></section>
<section class="panel"><div class="chart-head"><h2>Paper trading · skutočné ceny (USD)</h2><button class="secondary" id="market-paper-check" type="button">Overiť nový snapshot</button></div><div class="actions"><button id="market-paper-start" type="button">Spustiť USD paper</button><button class="secondary" id="market-paper-pause" type="button">Pauza USD</button><button class="danger" id="market-paper-stop" type="button">Núdzové STOP USD</button></div><div id="market-paper-status" class="sub" style="margin:10px 0">Načítavam…</div><div class="stat-grid" id="market-paper-cards"></div><div id="market-paper-signals" class="sub" style="margin-top:12px"></div><div id="market-paper-positions" class="sub" style="margin-top:10px"></div><div id="market-paper-profit-check" class="sub" style="margin-top:10px"></div><div id="market-paper-trades" class="sub" style="margin-top:10px"></div><div id="intraday-report" style="margin-top:14px"></div><div id="intraday-decisions" class="sub" style="margin-top:12px"></div><small>Oddelený USD testovací účet. Testovacia momentum stratégia otvára virtuálne pozície iba po samostatnom stlačení Spustiť USD paper; overené ceny spracúva približne každých 5 minút a nové pozície majú maximálne 30-minútové držanie. Pauza zakáže nové vstupy, otvorené pozície sa uzatvárajú podľa TP/SL/času. Núdzové STOP zablokuje nové vstupy a uzavrie virtuálne pozície až pri ďalšej overenej cene, nie okamžite. Spread 10 bps, slippage 5 bps a poplatok 0 USD sú iba pracovné predpoklady. Pred každým virtuálnym vstupom sa kontroluje čistý potenciál TP a pri uzatvorení čistý P/L; stratové ochranné výstupy sa nesmú blokovať. Žiadna konverzia EUR/USD ani spojenie s Trading 212 objednávkami.</small></section>
<section class="panel"><div class="chart-head"><h2>Trading 212 · Demo účet</h2><button id="t212-check" class="secondary" type="button">Overiť Demo API</button></div><div id="t212-status" class="sub" style="margin-top:12px">Kontrolujem konfiguráciu…</div><div id="t212-result" style="margin-top:10px"></div><div class="panel" style="margin-top:14px">
<h2>Trading 212 Demo · manuálny test objednávky</h2>
<p class="sub">Iba DEMO. Pred odoslaním musíš potvrdiť každú limitnú objednávku. Max. 25 jednotiek meny účtu na nákup a 100 celkovo na testovanie. Žiadna automatizácia ani Live objednávky.</p>
<div class="actions"><button type="button" class="secondary" id="demo-catalog-load">Načítať dostupné nástroje</button><button type="button" class="secondary" id="demo-desk-refresh">Skontrolovať objednávky</button></div>
<div class="stat-grid">
<label class="stat">Ticker<select id="demo-ticker" style="display:block;width:100%;padding:10px;margin-top:7px;background:#0b1120;border:1px solid #41506a;border-radius:7px;color:white"><option value="">Najprv načítaj nástroje</option></select></label>
<label class="stat">Smer<select id="demo-side" style="display:block;width:100%;padding:10px;margin-top:7px;background:#0b1120;border:1px solid #41506a;border-radius:7px;color:white"><option value="BUY">BUY – nákup</option><option value="SELL">SELL – predaj</option></select></label>
<label class="stat">Počet kusov<input id="demo-qty" type="text" inputmode="decimal" autocomplete="off" placeholder="0,1" style="display:block;width:100%;padding:10px;margin-top:7px;background:#0b1120;border:1px solid #41506a;border-radius:7px;color:white"></label>
<label class="stat">Limitná cena (mena nástroja)<input id="demo-limit-price" type="text" inputmode="decimal" autocomplete="off" placeholder="100,00" style="display:block;width:100%;padding:10px;margin-top:7px;background:#0b1120;border:1px solid #41506a;border-radius:7px;color:white"></label>
</div>
<div class="actions"><button id="demo-submit-limit" type="button" disabled>Potvrdiť DEMO limitnú objednávku</button></div>
<div id="demo-order-message" role="status" class="sub"></div><div id="demo-broker-overview" class="sub" style="margin-top:10px"></div><div id="demo-order-log" class="sub" style="margin-top:10px"></div>
<small>Objednávka môže zostať čakajúca alebo sa nevyplniť. Ak API odpovie nejasne, bot ju automaticky neopakuje. Stav musíš skontrolovať v Trading 212 Demo.</small>
</div><small>Samostatné DEMO prostredie. Kontrola účtu je iba na čítanie; nižšie môžeš výslovne potvrdiť malé DEMO limitné objednávky. Automatické nákupy a Live objednávky sú vypnuté.</small></section>
<section class="panel"><h2>Skutočný trh – iba sledovanie</h2><div class="sub" id="market-status">Načítavam zdroj cien…</div><div id="market-diagnostics" class="sub" style="margin-top:9px"></div><div id="market-quotes" style="margin-top:12px"></div><small>Reálne ceny sú v USD. Kým nevyriešime FX, spread a aktuálnosť feedu, tieto ceny NEOVPLYVŇUJÚ simulované obchody.</small></section>
<section class="panel"><h2>Limity a testovací trh</h2><div id="limits" class="sub"></div><div id="prices" class="sub" style="margin-top:10px"></div><div id="updated" class="sub" style="margin-top:10px"></div><div id="risk" style="margin-top:12px"></div></section>
<section class="panel"><h2>Výsledky simulovaných obchodov</h2><div id="stats" class="stat-grid">Načítavam…</div><small>Úspešnosť a drawdown sú len zo syntetického simulátora. Bez reálnych cien, poplatkov a spreadu.</small></section>
<section class="panel"><h2>Otvorené pozície</h2><table><thead><tr><th>Symbol</th><th>Počet</th><th>Vstup</th><th>Aktuálna cena</th><th>P/L</th><th>Zostáva</th></tr></thead><tbody id="positions"></tbody></table></section>
<section class="panel"><h2>Ukončené obchody</h2><table><thead><tr><th>Čas (UTC)</th><th>Symbol</th><th>Vstup</th><th>Výstup</th><th>P/L</th><th>Dôvod</th></tr></thead><tbody id="trades"></tbody></table></section>
<section class="panel"><h2>Log rozhodnutí</h2><div id="events" class="sub"></div></section>
<footer>V0.6.1 CLOUD: ceny sú syntetické, stratégia nie je AI, žiadne objednávky neodchádzajú brokerovi. Cloudflare cron vykoná najviac jeden simulačný krok za minútu a čas vykonania nie je garantovaný. Simulované TP/SL môžu byť pri testovaní prekročené medzi tickmi. Denný limit straty blokuje nové obchody, nie všetky možné straty. V pôvodnom syntetickom režime nie sú reálne trhové ceny ani transakčné náklady. Samostatný USD trhový režim otvára virtuálne obchody AAPL/MSFT/NVDA iba po samostatnom spustení; obchodovanie na burze ani u brokera neprebieha.</footer>
<script src="/client.js?v=0.6.1"></script></body></html>`;

export const CLIENT = `const APP_VERSION='0.6.1';
const $=id=>document.getElementById(id),eur=x=>Number(x).toFixed(2)+' €';
const esc=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pl=x=>'<span class="'+(Number(x)>0?'green':Number(x)<0?'red':'neutral')+' pnl-cell">'+(Number(x)>0?'+':'')+eur(x)+'</span>';
let pending=false,brokerChecked=false;
async function checkVersion(){
 try{
  const r=await fetch('/health?v='+Date.now(),{cache:'no-store'});
  if(!r.ok)throw Error('HTTP '+r.status);
  const data=await r.json(),remote=String(data.version||'');
  const match=remote.match(/^([0-9]+[.][0-9]+[.][0-9]+)/);
  const server=match?match[1]:'neznáma';
  $('version-check').textContent=server===APP_VERSION?'Aktuálna v'+APP_VERSION:'Server v'+server+' · aktualizuj';
  $('version-check').className=server===APP_VERSION?'green':'red';
 }catch(e){$('version-check').textContent='Verziu sa nepodarilo overiť';$('version-check').className='red'}
}
$('force-update').onclick=async()=>{
 const button=$('force-update');button.disabled=true;button.textContent='Aktualizujem…';
 try{
  await fetch('/health?v='+Date.now(),{cache:'no-store'});
 }catch(e){/* Navigation still retries the newest HTML. */}
 const url=new URL('/',location.origin);
 url.searchParams.set('force_update',String(Date.now()));
 location.replace(url.toString());
};
checkVersion();
let versionPoll=setInterval(checkVersion,60000);

let range='24h',allChartPoints=[],renderedPoints=[],selectedIndex=null;
const windows={'1h':3600000,'24h':86400000,'7d':604800000};
const stamp=x=>{const t=Date.parse(x.at||'');return Number.isFinite(t)?t:NaN};
const dateLabel=(ms,wide=false)=>new Date(ms).toLocaleString('sk-SK',wide?{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}:{hour:'2-digit',minute:'2-digit'});
const svgText=(x,y,label,anchor='start')=>'<text x="'+x+'" y="'+y+'" text-anchor="'+anchor+'" fill="#9ba9c4" font-size="13">'+label+'</text>';
function draw(points){
  allChartPoints=Array.isArray(points)?points:[];
  const svg=$('chart'),cutoff=Date.now()-windows[range];
  let data=allChartPoints.filter(p=>Number.isFinite(stamp(p))&&Number.isFinite(Number(p.equity))&&stamp(p)>=cutoff&&stamp(p)<=Date.now()+120000);
  data.sort((a,b)=>stamp(a)-stamp(b));
  renderedPoints=data;
  $('chart-note').textContent='Časová os v miestnom čase · nové záznamy každé 2 obchodné ticky. Staršie záznamy bez času sa do intervalov nezapočítavajú.';
  if(data.length<2){
    const width=Math.max(280,svg.clientWidth||440);svg.setAttribute('viewBox','0 0 '+width+' 205');svg.innerHTML=svgText(width/2,96,'Zatiaľ málo dát pre '+range.toUpperCase(),'middle');
    $('chart-readout').textContent='Pre toto obdobie potrebujeme aspoň 2 časové záznamy.';
    return;
  }
  const width=Math.max(280,svg.clientWidth||440),left=69,right=width-13,plotWidth=right-left;
  svg.setAttribute('viewBox','0 0 '+width+' 205');
  const vals=data.map(p=>Number(p.equity)),lo=Math.min(...vals),hi=Math.max(...vals);
  const pad=Math.max((hi-lo)*.12,0.02),min=lo-pad,max=hi+pad;
  const first=stamp(data[0]),last=stamp(data[data.length-1]),span=Math.max(1,last-first);
  const xAt=p=>left+(stamp(p)-first)/span*plotWidth;
  const yAt=p=>169-(Number(p.equity)-min)/(max-min)*139;
  const coords=data.map(p=>xAt(p).toFixed(2)+','+yAt(p).toFixed(2)).join(' ');
  const rising=vals.at(-1)>=vals[0],stroke=rising?'#64deb1':'#ff8b97';
  let out='';
  for(let i=0;i<3;i++){
    const y=30+i*69.5,value=max-i*(max-min)/2;
    out+='<line x1="'+left+'" y1="'+y+'" x2="'+right+'" y2="'+y+'" stroke="#304057" stroke-dasharray="3 7"/>'+svgText(left-5,y-5,value.toFixed(2)+' €','end');
  }
  out+='<polyline points="'+coords+'" fill="none" stroke="'+stroke+'" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>';
  out+=svgText(left,197,dateLabel(first,range==='7d'))+(width>520?svgText((left+right)/2,197,dateLabel(first+span/2,range==='7d'),'middle'):'')+svgText(right,197,dateLabel(last,range==='7d'),'end');
  if(selectedIndex!==null&&data[selectedIndex]){
    const p=data[selectedIndex],x=xAt(p),y=yAt(p);
    out+='<line x1="'+x+'" x2="'+x+'" y1="25" y2="169" stroke="#8193b2" stroke-dasharray="4 4"/><circle cx="'+x+'" cy="'+y+'" r="5" stroke="#0b1120" stroke-width="2" fill="'+stroke+'"/>';
  }
  svg.innerHTML=out;
  const chosen=selectedIndex===null?data.at(-1):data[Math.min(selectedIndex,data.length-1)];
  $('chart-readout').textContent=dateLabel(stamp(chosen),true)+' · '+eur(chosen.equity)+(selectedIndex===null?' (posledný záznam)':'');
}
function choosePoint(event){
  if(!renderedPoints.length)return;
  const bounds=$('chart').getBoundingClientRect();
  if(!bounds.width)return;
  const width=Math.max(280,bounds.width),left=69,right=width-13,plotWidth=right-left;
  const x=(event.clientX-bounds.left)/bounds.width*width;
  const first=stamp(renderedPoints[0]),last=stamp(renderedPoints.at(-1)),span=Math.max(1,last-first);
  let best=0,distance=Infinity;
  for(let i=0;i<renderedPoints.length;i++){const px=left+(stamp(renderedPoints[i])-first)/span*plotWidth,d=Math.abs(px-x);if(d<distance){distance=d;best=i}}
  selectedIndex=best;draw(allChartPoints);
}
$('chart').addEventListener('pointerdown',choosePoint);
$('chart').addEventListener('pointermove',e=>{if(e.buttons===1)choosePoint(e)});
document.querySelectorAll('[data-range]').forEach(b=>b.addEventListener('click',()=>{range=b.dataset.range;selectedIndex=null;document.querySelectorAll('[data-range]').forEach(x=>x.classList.toggle('active',x===b));draw(allChartPoints)}));
async function refresh(){if(pending)return;try{const r=await fetch('/api/state',{cache:'no-store'});if(r.status===401){location.href='/';return}if(!r.ok)throw Error('HTTP '+r.status);const result=await r.json(),s=result.state;
$('equity').textContent=eur(s.equity);$('cash').textContent=eur(s.cash);$('invested').textContent=eur(s.invested);
$('pnl').innerHTML=pl(s.pnl);$('status').textContent=s.status;$('status').className='value '+(s.halted?'red':s.running?'green':'neutral');
$('limits').textContent='Počiatočný kapitál: '+eur(s.initial)+' · Riziko/obchod: '+s.limits.risk_per_trade_pct+' % · Denný limit straty: '+s.limits.daily_loss_pct+' % · Stop-loss: '+s.limits.stop_loss_pct+' % · Take-profit: '+s.limits.take_profit_pct+' % · Max. trvanie: '+(s.limits.max_hold_minutes||15)+' min · Max. pozície: '+s.limits.max_positions+' · Max. alokácia/pozícia: '+s.limits.max_allocation_pct+' %';
$('prices').textContent='Syntetické ceny: '+Object.entries(s.prices).map(([k,v])=>k+': '+Number(v).toFixed(2)).join(' · ')+' · Tick '+s.tick;
const st=s.stats||{};
$('stats').innerHTML=[
 ['Obchody',s.trade_count],
 ['Win rate',st.win_rate==null?'–':st.win_rate+' %'],
 ['Profit factor',st.profit_factor==null?(s.trade_count===0?'Nedostatok dát':s.losses===0?'Bez strát':'Nedostatok dát'):st.profit_factor],
 ['Uzavreté P/L',eur(st.closed_pnl||0)],
 ['Max. drawdown',(st.max_drawdown_pct??0)+' %']
].map(([k,v])=>'<div class="stat"><small>'+esc(k)+'</small><strong>'+esc(v)+'</strong></div>').join('');
const used=Math.max(0,Math.min(100,(s.committed||0)/s.initial*100));
$('risk').innerHTML='<div class="sub">Záväzne alokované: '+eur(s.committed||0)+' / '+eur(s.initial)+' ('+used.toFixed(1)+' %) · Denná rezerva do straty: '+eur(s.daily_loss_remaining||0)+'</div><div class="risk-meter"><div class="risk-fill '+(s.daily_loss_hit?'warn':'')+'" style="width:'+used+'%"></div></div><small>'+(s.daily_loss_hit?'Denný limit straty dosiahnutý – nové obchody zablokované.':'Limit vstupného kapitálu sa kontroluje pred každým nákupom.')+'</small>';


$('updated').textContent='Posledný zápis: '+(result.updatedAt||'Zatiaľ žiadny')+' · Kontroly cca raz/min';
const mp=result.marketPaper||{};
$('market-paper-status').textContent='Stav feedu: '+(mp.reason||'Neznámy')+' · Posledný spracovaný snapshot: '+(mp.lastSnapshotAt?new Date(mp.lastSnapshotAt).toLocaleString('sk-SK'):'žiadny')+' · Obchodovanie: '+(mp.halted?'STOP / čaká na overenú cenu':mp.paused?'PAUZA':'BEŽÍ')+' · Otvorené: '+(mp.positions?.length||0);
$('market-paper-cards').innerHTML=[['Testovací kapitál',mp.initial],['Hotovosť USD',mp.cash],['Hodnota USD',mp.equity],['P/L USD',mp.pnl],['Otvorené pozície',mp.positions?.length??0]].map(([k,v])=>'<div class="stat"><small>'+esc(k)+'</small><strong>'+esc(v==null?'–':typeof v==='number'?v.toFixed(2):v)+'</strong></div>').join('');
$('market-paper-positions').innerHTML='<b>USD otvorené pozície</b><div>'+((mp.positions||[]).map(p=>'<div style="padding:6px 0;border-bottom:1px solid #2b3953">'+esc(p.symbol)+' · '+esc(p.qty)+' ks · vstup '+Number(p.entry).toFixed(2)+' USD · cena '+Number(p.mark).toFixed(2)+' USD</div>').join('')||'Žiadne pozície')+'</div>';
$('market-paper-profit-check').innerHTML='<b>Kontrola potenciálneho zisku</b><div>'+((mp.positions||[]).map(p=>{
 const a=p.entryAssessment||{};
 return '<div style="padding:6px 0;border-bottom:1px solid #2b3953">'+esc(p.symbol)+' · čistý TP '+(a.netTargetUsd==null?'–':Number(a.netTargetUsd).toFixed(2)+' USD')+' · čistý SL '+(a.netStopUsd==null?'–':Number(a.netStopUsd).toFixed(2)+' USD')+' · potrebná úspešnosť '+(a.breakEvenWinRatePct==null?'–':Number(a.breakEvenWinRatePct).toFixed(1)+' %')+'</div>'
 }).join('')||'Bez otvorených pozícií')+'</div><small>Ide o modelované scenáre po predpokladaných nákladoch, nie predikciu zisku ani overenú úspešnosť stratégie.</small>';
$('market-paper-trades').innerHTML='<b>USD uzavreté obchody ('+(mp.tradeCount||0)+')</b><div>'+((mp.trades||[]).slice(0,8).map(t=>'<div style="padding:6px 0;border-bottom:1px solid #2b3953">'+esc(t.symbol)+' · '+esc(t.reason)+' · <span class="'+(t.pnl>0?'green':'red')+'">'+Number(t.pnl).toFixed(2)+' USD</span></div>').join('')||'Žiadne obchody')+'</div>';

const perf=mp.performance||{},moneyUSD=x=>(Number.isFinite(Number(x))?Number(x).toFixed(2)+' USD':'–');
$('intraday-report').innerHTML='<h2>Intraday výsledky · USD paper</h2><div class="stat-grid">'+[
 ['Ukončené obchody',(perf.sampleSize??0)+' / '+(perf.targetSample??100)],
 ['Čisté P/L',moneyUSD(perf.netPnlUsd)],
 ['Úspešnosť',perf.winRatePct==null?'–':Number(perf.winRatePct).toFixed(1)+' %'],
 ['Profit factor',perf.profitFactor==null?'–':Number(perf.profitFactor).toFixed(2)],
 ['Priemerný obchod',perf.averageTradeUsd==null?'–':moneyUSD(perf.averageTradeUsd)],
 ['Priemerné držanie',perf.averageHoldMinutes==null?'–':perf.averageHoldMinutes+' min'],
 ['Max. séria strát',perf.maxConsecutiveLosses??0],
 ['Overenie stratégie','NEOVERENÁ']
].map(([k,v])=>'<div class="stat"><small>'+esc(k)+'</small><strong>'+esc(v)+'</strong></div>').join('')+'</div><div style="margin-top:12px">'+(perf.bySymbol||[]).map(x=>'<div style="padding:7px 0;border-bottom:1px solid #2b3953"><b>'+esc(x.symbol)+'</b> · '+Number(x.trades||0)+' obchodov · '+moneyUSD(x.netPnlUsd)+' · úspešnosť '+(x.winRatePct==null?'–':x.winRatePct+' %')+'</div>').join('')+'</div><small>Výsledky sú len z virtuálnych obchodov. Počet 100 je minimálny orientačný cieľ, nie dôkaz ziskovosti.</small>';
$('intraday-decisions').innerHTML='<h2>Posledné rozhodnutia · USD paper</h2>'+((mp.decisions||[]).slice(0,9).map(d=>'<div style="padding:6px 0;border-bottom:1px solid #2b3953">'+esc(d.symbol)+' · '+esc(d.signal)+' · '+esc(d.outcome)+' · '+esc(new Date(d.at).toLocaleString('sk-SK'))+'</div>').join('')||'Čakáme na prvé nové cenové údaje.');
const broker=result.broker||{};
if(!brokerChecked)$('t212-status').textContent=broker.configured?'Demo kľúče nastavené · Klikni Overiť Demo API':'Nepripojené · Pre test nastav T212_DEMO_API_KEY a T212_DEMO_API_SECRET v Cloudflare Secrets';
const m=result.market||{},isOpen=!!m.sessionOpen;
const statuses={MARKET_CLOSED:'Trh zatvorený',FEED_READY:'Zdroj pripravený – iba sledovanie',AWAITING_FIRST_CHECK:'Čaká sa na prvé overenie',PROVIDER_OR_DATA_ERROR:'API alebo dáta hlásia chybu',STALE_OR_UNVERIFIED:'Ceny nie sú overené ako aktuálne'};
const description=m.configured?(statuses[m.diagnostic]||'Čaká sa na diagnostiku'):'API kľúč nie je nastavený';
$('market-status').textContent='Twelve Data · '+description+' · '+(isOpen?'Bežná US seansa':'Mimo bežnej US seansy');
$('market-status').className='sub '+(m.diagnostic==='FEED_READY'?'green':m.diagnostic==='PROVIDER_OR_DATA_ERROR'?'red':'neutral');
$('market-diagnostics').textContent='Pokusy: '+(m.attempts??0)+' · Úspešné kompletné overenia: '+(m.successes??0)+' · Posledný pokus: '+(m.lastAttemptAt?new Date(m.lastAttemptAt).toLocaleString('sk-SK'):'zatiaľ žiadny')+' · Posledné úspešné: '+(m.lastSuccessAt?new Date(m.lastSuccessAt).toLocaleString('sk-SK'):'zatiaľ žiadne')+' · '+(m.notice||'');
$('market-quotes').innerHTML=(m.quotes||[]).map(q=>'<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #2b3953"><b>'+esc(q.symbol)+'</b><span style="text-align:right">'+Number(q.price).toFixed(2)+' USD · <span class="'+(q.fresh?'green':'red')+'">'+(q.fresh?'Aktuálne podľa zdroja':'Neoverené / zastarané')+'</span><div class="sub">'+esc(new Date(q.timestamp*1000).toLocaleString('sk-SK'))+' · '+esc(q.timestampSource==='bar_open'?'Čas sviečky, nie live cena':'Čas poslednej kotácie')+'</div></span></div>').join('')||'<div class="sub">Zatiaľ žiadne overené ceny. '+esc(!m.configured?'Nastav TWELVEDATA_API_KEY v Cloudflare Secrets.':'Počkaj na prvé načítanie počas obchodnej seansy.')+'</div>';
if((m.errors||[]).length)$('market-diagnostics').textContent+=' · Chyby: '+m.errors.join('; ');

$('positions').innerHTML=s.positions.map(p=>'<tr><td>'+esc(p.symbol)+'</td><td>'+p.qty+'</td><td>'+eur(p.entry)+'</td><td>'+eur(p.market)+'</td><td>'+pl(p.unrealized)+'</td><td>'+Math.ceil((p.remaining_seconds||0)/60)+' min</td></tr>').join('')||'<tr><td colspan="6">Žiadne otvorené pozície</td></tr>';
$('trades').innerHTML=s.trades.map(t=>'<tr><td>'+esc(t.at)+'</td><td>'+esc(t.symbol)+'</td><td>'+eur(t.entry)+'</td><td>'+eur(t.exit)+'</td><td>'+pl(t.pnl)+'</td><td>'+esc(t.reason)+'</td></tr>').join('')||'<tr><td colspan="6">Zatiaľ žiadne obchody</td></tr>';
$('events').innerHTML=s.events.map(e=>'<div style="padding:5px 0">'+esc(e.at)+' · '+esc(e.kind)+' · '+esc(e.message)+'</div>').join('')||'Žiadne udalosti';draw(s.equity_history);
}catch(e){$('message').textContent='Chyba načítania: '+e.message}}
for(const [id,action] of [['market-paper-start','start'],['market-paper-pause','pause'],['market-paper-stop','stop']]){
 $(id).onclick=async()=>{
  if(action==='stop'&&!confirm('Zablokovať nové USD paper obchody? Otvorené virtuálne pozície sa uzavrú až pri najbližšej overenej cene.'))return;
  $(id).disabled=true;
  try{
   const r=await fetch('/api/market-paper/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
   const result=await r.json();
   $('market-paper-signals').textContent=result.ok?'USD paper: '+action+' potvrdené':'USD paper chyba: '+(result.reason||result.message||r.status);
   await refresh();
  }catch(e){$('market-paper-signals').textContent='USD paper chyba: '+e.message}
  finally{$(id).disabled=false}
 };
}
$('market-paper-check').onclick=async()=>{
 const b=$('market-paper-check');b.disabled=true;$('market-paper-signals').textContent='Overujem snapshot…';
 try{
  const r=await fetch('/api/market-paper/check',{method:'POST',headers:{'Content-Type':'application/json'}});
  const d=await r.json();
  $('market-paper-signals').textContent=d.ok?'Snapshot spracovaný · '+(d.signals||[]).map(s=>s.symbol+': '+s.action+' ('+Number(s.price).toFixed(2)+' USD)').join(' · '):'Kontrola zamietnutá: '+(d.reason||d.message||'chyba');
  await refresh();
 }catch(e){$('market-paper-signals').textContent='Chyba: '+e.message}
 finally{b.disabled=false}
};
// Slovak iOS decimal keyboard uses comma. Keep text inputs until final validation.
function parseDemoDecimal(raw){
 const text=String(raw??'').trim().replace(',', '.');
 if(!/^(?:[0-9]+(?:[.][0-9]*)?|[.][0-9]+)$/.test(text))return NaN;
 return Number(text);
}
let demoCurrency='',demoCatalogReady=false;
const demoPost=async (url,body={})=>{
 const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'});
 const result=await r.json();return result;
};
async function refreshDemoDesk(){
 try{
  const r=await fetch('/api/t212/demo/desk',{cache:'no-store'});
  if(!r.ok)throw Error('HTTP '+r.status);
  const data=await r.json();
  $('demo-order-log').innerHTML='<b>Posledné odoslania DEMO</b><div>'+((data.orders||[]).map(o=>'<div style="padding:6px 0;border-bottom:1px solid #2b3953">'+esc(o.side)+' '+esc(o.ticker)+' · '+esc(o.quantity)+' @ '+esc(o.limit_price)+' · '+esc(o.state)+(o.broker_id?' · T212 #'+esc(o.broker_id):'')+'</div>').join('')||'Žiadne DEMO objednávky')+'</div>';
 }catch(e){$('demo-order-log').textContent='Nedá sa načítať história: '+e.message}
}
async function refreshBrokerOverview(){
 try{
  const r=await fetch('/api/t212/demo/overview',{cache:'no-store'});
  const d=await r.json();
  if(!d.ok)throw Error(d.reason||'DEMO_UNAVAILABLE');
  demoCurrency=d.account.currency||'';
  $('demo-broker-overview').innerHTML='<b>Trading 212 DEMO účet</b><div>Hlavná mena: '+esc(demoCurrency)+' · hotovosť: '+Number(d.account.availableToTrade).toFixed(2)+' '+esc(demoCurrency)+'</div>'+
   '<div>Broker pozície: '+(d.positions||[]).length+' · čakajúce objednávky: '+(d.pending||[]).length+'</div>'+
   (d.positions||[]).slice(0,6).map(p=>'<div style="padding:5px 0">'+esc(p.ticker)+' · '+Number(p.quantity).toFixed(4)+' ks · aktuálna cena '+Number(p.currentPrice).toFixed(2)+'</div>').join('')+
   (d.pending||[]).slice(0,6).map(p=>'<div style="padding:5px 0;color:#ffbd7c">Čaká: '+esc(p.side)+' '+esc(p.ticker)+' · '+esc(p.status)+'</div>').join('');
 }catch(e){$('demo-broker-overview').textContent='Broker dáta sa nedajú načítať: '+e.message}
}
$('demo-desk-refresh').onclick=async()=>{
 $('demo-desk-refresh').disabled=true;
 try{await refreshBrokerOverview();await refreshDemoDesk()}
 finally{$('demo-desk-refresh').disabled=false}
};
$('demo-catalog-load').onclick=async()=>{
 const b=$('demo-catalog-load');b.disabled=true;$('demo-order-message').textContent='Overujem nástroje v T212 Demo…';
 try{
  const result=await demoPost('/api/t212/demo/catalog');
  if(!result.ok)throw Error(result.reason||'Nedostupný katalóg');
  const select=$('demo-ticker');
  select.innerHTML='<option value="">Vyber ticker</option>'+result.items.map(i=>'<option value="'+esc(i.ticker)+'">'+esc(i.name)+' · '+esc(i.ticker)+' · '+esc(i.currencyCode)+'</option>').join('');
  demoCatalogReady=true;
  $('demo-submit-limit').disabled=false;
  $('demo-order-message').textContent='Načítaných '+result.items.length+' akcií/ETF · Vyber ticker s menou zhodnou s účtom.';
 }catch(e){demoCatalogReady=false;$('demo-submit-limit').disabled=true;$('demo-order-message').textContent='Katalóg: '+e.message}
 finally{b.disabled=false}
};
$('demo-submit-limit').onclick=async()=>{
 const b=$('demo-submit-limit'),ticker=$('demo-ticker').value,side=$('demo-side').value,
  quantity=parseDemoDecimal($('demo-qty').value),limitPrice=parseDemoDecimal($('demo-limit-price').value);
 if(!demoCatalogReady||!ticker||!Number.isFinite(quantity)||!Number.isFinite(limitPrice)||quantity<=0||limitPrice<=0){$('demo-order-message').textContent='Vyber ticker a zadaj kladné čísla. Môžeš použiť čiarku aj bodku (napr. 0,1).';return}
 const sum=quantity*limitPrice;
 if(side==='BUY'&&sum>25.000001){$('demo-order-message').textContent='Nákup prekračuje 25 jednotiek meny účtu.';return}
 if(!confirm('Odovzdať '+side+' '+quantity+' × '+ticker+' za limit '+limitPrice+' na Trading 212 DEMO? Toto je SKUTOČNÁ demo objednávka (nie SIM).'))return;
 b.disabled=true;
 const intentId=crypto.randomUUID();
 $('demo-order-message').textContent='Odosielam 1× DEMO limitnú objednávku…';
 try{
  const result=await demoPost('/api/t212/demo/limit',{intentId,ticker,side,quantity,limitPrice});
  $('demo-order-message').textContent=result.ok?'DEMO prijalo objednávku #'+result.brokerId+' – čakaj na vyplnenie.':
   'Objednávka nepotvrdená: '+(result.reason||'UNKNOWN')+(result.reason==='BROKER_ORDER_UNCERTAIN_DO_NOT_RETRY'?' · Neopakovať! Over priamo v T212.':'');
  await refreshDemoDesk();
 }catch(e){$('demo-order-message').textContent='Nejasná odpoveď – neposielaj objednávku znova. Over T212 Demo a históriu: '+e.message}
 finally{b.disabled=false}
};
refreshDemoDesk();
$('t212-check').onclick=async()=>{
 const b=$('t212-check');b.disabled=true;$('t212-status').textContent='Overujem Demo API…';
 try{
  const r=await fetch('/api/t212/demo',{method:'POST',headers:{'Content-Type':'application/json'}});
  if(!r.ok)throw Error('HTTP '+r.status);
  const s=await r.json();brokerChecked=true;
  $('t212-status').textContent=s.connected?'Demo účet pripojený · Kontrola '+new Date(s.checkedAt).toLocaleString('sk-SK'):'Demo nepripojené · '+(s.error||s.reason||'Neznáma chyba');
  $('t212-result').innerHTML=s.connected?'<div class="stat-grid">'+[['Hodnota Demo účtu',s.account.totalValue],['Voľná hotovosť',s.account.availableToTrade],['Investície',s.account.investedValue],['Počet pozícií',s.positionCount]].map(([k,v])=>'<div class="stat"><small>'+esc(k)+'</small><strong>'+esc(v==null?'–':v)+(k==='Počet pozícií'?'':' '+esc(s.account.currency||''))+'</strong></div>').join('')+'</div><div class="sub" style="margin-top:10px">Pozície: '+(s.positions.map(p=>esc(p.ticker)+' ('+esc(p.quantity)+')').join(' · ')||'Žiadne')+'</div>':'';
 }catch(e){$('t212-status').textContent='Chyba kontroly: '+e.message}
 finally{b.disabled=false}
};
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{const a=b.dataset.action;if(['stop','reset'].includes(a)&&!confirm(a==='stop'?'Okamžite uzavrieť všetky syntetické pozície a zablokovať obchodovanie?':'Vymazať demo históriu a obnoviť počiatočný kapitál?'))return;pending=true;try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});const d=await r.json();$('message').textContent=r.ok?'':d.message||'Chyba';}catch(e){$('message').textContent=e.message}finally{pending=false;refresh()}});
$('logout').onclick=async()=>{await fetch('/api/logout',{method:'POST'});location.href='/'};
refresh();setInterval(refresh,12000);`;
