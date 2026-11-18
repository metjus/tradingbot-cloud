# Zoznam zmien

## ARGUS CFE (samostatná verzia, vetva claude/argus-cfe-strategy)

Stratégia CFE z výskumu ako samostatný paper účet a samostatná aplikácia
(vedľa hlavného ARGUS). Podrobnosti, testy a backtest: docs/CFE.md.
Backtest: 1 448 obchodov za 2 roky, −0,022 R na obchod po poplatkoch -
iba paper.

## 1.2.x - graf po kliknutí na pozíciu, bezpečnejšie zatváranie z mobilu

- **Klik na otvorenú pozíciu zobrazí graf** - vo Windows appke sa hlavný
  graf prepne na danú mincu (timeframe podľa stratégie) a nakreslí vstup,
  stop-loss, take-profit a likvidáciu; v mobile sa otvorí okno so živým
  grafom (15m / 1h / 4h / 1D) priamo z Kraken Futures.
- **Zatvorenie jednej pozície z mobilu** už neprepína na obrazovku
  Ovládanie (kde je tlačidlo EMERGENCY STOP); výsledok sa ukáže priamo
  pri pozícii. Celá cesta mobil -> cloud -> počítač bola overená testom:
  zatvorí sa iba vybraná pozícia.
- **EMERGENCY STOP v mobile treba potvrdiť napísaním STOP** - náhodný
  dotyk ho už nespustí. V histórii obchodov sa zapisuje ako
  „emergency_stop“ (doteraz „manual_close“, nedalo sa to rozlíšiť).
- Vzdialený take-profit shortu už nemôže vyjsť záporný.
- **Mobil -> Ovládanie -> „Posledné príkazy z telefónu“**: presne čo telefón
  poslal (zatvoriť 1 pozíciu / EMERGENCY STOP / pauza / štart), kedy a koľko
  pozícií sa zatvorilo - vrátane starších príkazov.
- **Cloud odmietne EMERGENCY STOP bez napísaného STOP** - platí aj pre
  starú verziu appky uloženú v telefóne.

## 1.2.x - aktívny režim = trendové portfólio (~13 obchodov denne, v pluse)

Predchádzajúci aktívny režim (jeden PSAR na 4h) robil obchody, ale po
poplatkoch bol na nule. Nahradilo ho **trendové portfólio**:
- **12 systémov** (Supertrend, Parabolic SAR, EMA cross, Donchian) na
  2h, 4h, 6h a 8h grafoch, väčšina s filtrom EMA200,
- každý systém drží pozíciu na minci, kým trend trvá, a pri otočení ju
  zavrie (ak filter dovolí, otvorí opačnú); bezpečnostný stop 3 ATR,
- obchoduje **~20 najobchodovanejších mincí**, vstupy aj výstupy marketom,
- veľkosť pozícií sa prispôsobuje **volatilite trhu** (×0,25 až ×1,5).

**Ako bolo vybrané (bez prikrášľovania):** 240 systémov otestovaných na
2 rokoch dát Kraken Futures s market vstupom aj výstupom, sklzom
a fundingom. 12 systémov bolo vybraných **iba podľa prvého roka**;
druhý rok ich výber nevidel. V druhom roku mal každý z nich PF nad 1,1,
spolu **PF 1,29 pri ~13 obchodoch denne**.

**Čo čakať pri 0,02 % riziku na obchod** (10 000 simulovaných rokov):
medián **+20 %/rok**, 5 % najhorších rokov -9 až -15 %, stratový rok
v 15-20 % prípadov, typický prepad 11-15 %. Pri 0,03 %: medián ~+30 %,
prepad 16-21 %. Beží popri hlavnej stratégii (tá má vlastné riziko).

**Treba vedieť:** úspešnosť je ~36 % - zarába málo veľkých obchodov
a veľa malých strát. Aj zdravý systém má v ~40 % mesiacov PF pod 1, preto
sa hodnotí na mesiace. Naraz býva otvorených ~100 malých pozícií.
Automatické vypnutie: čistá strata nad 25 % rozpočtu (alebo PF pod 1 po
3000 obchodoch). Pred ostrým obchodovaním budú potrebné sub-účty (burza
spája pozície na rovnakej minci).

## 1.2.x - aktívny režim: 5+ obchodov denne (paper)

Hlavná stratégia (trend_rider_4h na top 5 mincí) robí len pár obchodov
mesačne. Kto chce, aby bot obchodoval každý deň, má teraz **aktívny režim**:
- **Parabolic SAR na 4h grafe**, vždy v trhu - keď sa SAR otočí, pozícia
  sa obráti (long ↔ short); bezpečnostný stop 3 ATR,
- obchoduje **20 najobchodovanejších mincí** (objem nad 1 mil. $ denne,
  nastaviteľné 5-30), nielen top 5,
- na reálnych dátach za posledných 60 dní **~9 obratov denne** (12 mincí
  dalo 5,9 - preto 20),
- **vlastný účet** (vlastné štatistiky, nezaberá miesto hlavnej stratégii),
  **desatinové riziko** (0,2 % na obchod pri 2 %), max. 30 otvorených pozícií.

**Úprimne:** po poplatkoch vyšiel zhruba na nule (PF 1,14 v prvom roku,
1,03 v druhom) s veľkými výkyvmi. Nie je to stroj na peniaze - je to
aktivita s malým rizikom. Vypne sa sám, ak po 60 obchodoch prerába (PF pod
1,0) alebo strata presiahne 2 % rozpočtu. Dá sa vypnúť v Settings → Výstupy.

Pred ostrým obchodovaním: burza spája pozície na rovnakom kontrakte, takže
aktívny režim a hlavná stratégia na tej istej minci budú potrebovať
samostatné sub-účty.

## 1.2.x - zameranie: top 5 mincí, 1 obchod naraz, riziko 2 % s poistkou

**Oprava predchádzajúcich odhadov.** Prepočet presne tak, ako bot obchoduje
naživo (4h sviečky od 00:00 UTC, limity pozícií, zložené úročenie, 10 000
simulácií roka), ukázal, že skoršie čísla (~14-28 %/rok) boli príliš
optimistické. Pôvodné nastavenie (15 mincí, max. 4 pozície, 1 %): medián
asi +3 %/rok a 44 % šanca stratového roka.

**Nové nastavenie** (otestované bez pohľadu do budúcnosti - mince sa
vyberajú podľa objemu za posledných 30 dní, ako to robí bot):
- obchoduje sa iba **top 5 mincí podľa 30-dňového objemu v USD**, každý
  deň sa preradia; ďalšie 3 sa len sledujú,
- **1 pozícia naraz** (polovičné prepady pri vyššom výnose),
- **riziko 2 % na obchod** (~štvrtina Kellyho optima), strop 2 %,
- **poistka rizika**: ak naživo PF po 20 obchodoch klesne pod 1,0 alebo
  prepad dosiahne 20 %, riziko sa samo zníži na 1 %; späť pri PF 1,2+.

Výsledok simulácie pri 2 %: medián +31 %/rok, 5 % najhorších rokov
-13 %, šanca stratového roka 14 %, typický prepad 14 %, prepad nad 30 %
v 2 % prípadov. Neistota: 83 obchodov za 2 roky - PF je v 90 % intervale
0,8-2,7; poistka rizika je preto súčasťou nastavenia.

Ak si tieto hodnoty v Settings nikdy nemenil, aktualizácia ich nastaví
sama; vlastné hodnoty ostanú. Skúšobný režim a AI trader majú tiež
max. 1 pozíciu.

## 1.2.x - indikátory z TradingView pre AI tradera

- 10 najpopulárnejších indikátorov z TradingView prepísaných do bota
  (podľa ich zverejnených vzorcov): Supertrend, UT Bot, Parabolic SAR,
  Chandelier Exit, SSL Channel, Hull MA, Ichimoku, Squeeze Momentum
  (LazyBear), WaveTrend (LazyBear), MACD.
- Otestované ako samostatné stratégie na 2 rokoch dát (15 mincí, 15m/1h/4h,
  reálne poplatky): **ani jeden nie je lepší ako hlavná stratégia**; z 60
  variantov boli v pluse v oboch rokoch iba 3, a to len tesne (PF
  1,02-1,03), na 15m prerábajú všetky. Preto sa podľa nich priamo
  neobchoduje.
- AI trader ich aktuálny stav dostáva pri každom rozhodnutí (15m aj 1h)
  ako ďalší kontext - posúdi ich ako trader, ktorý sa pozrie na graf.

## 1.2.x - AI trader

- **AI trader**: jazykový model (Claude / GPT / Gemini podľa nastavenia)
  obchoduje sám ako skalper - prečíta si 15-min graf (12 h sviečok,
  trend 1h/4h, RSI, ATR, funding) a rozhodne, kam dať limitný príkaz
  (long pod cenou / short nad cenou), alebo nič. Sama si určí aj to, kedy
  sa pozrie znova (15 min v rýchlom trhu alebo blízko úrovne, až 4 h keď
  je trh pokojný); dovtedy platí jej limit, potom ho zruší a dá nový.
  Otvorenú pozíciu pri každom pohľade zváži: držať alebo zavrieť.
  - model navrhuje iba ceny; veľkosť, páku a limity určuje deterministický
    risk supervisor; navrhnuté ceny musia prejsť pevnými hranicami (limit
    max. 3 % od ceny, stop 0,25-4 %, zisk/riziko po poplatkoch aspoň 1,5)
  - vlastný účet: štvrtina bežného rizika, max. 2 pozície a 12 vstupov
    denne, poplatky max. 15 % rizika, vstup aj take-profit ako limit
  - automaticky sa vypne, ak po poplatkoch prerába (rovnako ako skúšobný
    režim) a pošle notifikáciu
  - dostáva iba čísla (ceny, indikátory), žiadny externý text
  - potrebuje AI kľúč (Settings → AI); predvolený model na obchodovanie
    je silnejší (Claude Sonnet, ~0,45 $/deň pri BTC)
- Denný rozpočet na AI zvýšený z 1 $ na **2 $** (AI trader + správy);
  ak si ho nikdy nemenil, aktualizácia ho zdvihne sama.
- Prečo AI a nie pravidlo: mechanická verzia tejto metódy (limit o kus
  lepšie po formujúcom sa prieraze/poklese) na 2 rokoch dát nezarobila
  v ani jednom z 84 variantov - rozhodujúci je úsudok, kedy a kde.
- Take-profit sa v paper režime účtuje ako maker poplatok (je to limitný
  príkaz); stop-loss a ručné zatvorenie ako taker.

## 1.2.x - skúšobný režim (obchody aj vo „vákuu")

- **Skúšobný režim**: kým hlavná stratégia čaká na obrat trendu, bot môže
  shortovať odraz v 4h poklese (cena sa vráti k 4h EMA21 a zatvorí späť
  pod ňou červenou sviečkou, pod EMA200, BTC klesá). Nie je to overený
  edge (v klesajúcom roku PF 1,05), preto:
  - riziko je **desatina** bežného (0,1 % namiesto 1 %),
  - vlastné limity: max. 2 pozície naraz a 3 vstupy za deň,
  - obchod sa neotvorí, ak by poplatky zjedli viac ako 5 % rizika,
  - **automaticky sa vypne**, ak po poplatkoch prerába (profit factor
    pod 1,0 po 20 obchodoch, alebo čistá strata nad 1 % rozpočtu) a
    pošle notifikáciu; hlavná stratégia beží ďalej.
  - nikdy neblokuje hlavnú stratégiu (vlastné počítadlá pozícií).
  Karta na dashboarde ukazuje výsledok po poplatkoch a zaplatené poplatky.
- Výber mincí vynecháva tokenizované akcie, komodity, stablecoiny a meme
  mince podľa kategórie, ktorú uvádza samotná burza.

## 1.2.x - panel „Čo bot sleduje", rýchlejší štart

- Nový panel **Čo bot sleduje** na dashboarde: pri každej minci 4h trend,
  vzdialenosť EMA21 od EMA50 (čím menšia, tým bližšie signál), aký smer
  by otvorilo najbližšie prekríženie a či ho filtre (EMA200, smer BTC)
  pustia; čas uzavretia ďalšej 4h sviečky a počet vyhodnotení. Tichý bot
  sa tak dá odlíšiť od pokazeného.
- Okno a API naskočia hneď; výber mincí a sťahovanie histórie bežia na
  pozadí (predtým sa čakalo desiatky sekúnd).
- Viac mincí: automatický výber berie kontrakty od 2 mil. $ denného objemu
  (predtým 5 mil.) - presne rozsah, na ktorom bola stratégia overená.
- **donchian_4h sa už neobchoduje**: s plnou sadou výstupov má profit
  factor ~1,0 (viac obchodov, žiadny edge, väčší prepad).
- Opravy: história pri štarte sa už neanalyzuje naprázdno; 4h sviečka
  z neúplného okna už neprepíše správnu sviečku z burzy.

## 1.2.x - objemový exit, výber AI modelu, lacnejšie maker vstupy

- **Riziko na obchod predvolene 1 %** (predtým 0,5 %): zodpovedá zhruba
  osmine Kellyho optima pre nameraný edge - rozumná agresivita s prepadmi
  okolo 20 %. Ak si hodnotu nikdy nemenil, aktualizácia ju zdvihne;
  vlastné nastavenie sa nemení. Strop ostáva 1 %.
- Predvolený počet mincí zvýšený z 10 na 15 - viac 4h signálov s overeným
  očakávaním. Obchodovanie na 1h/15m/5m grafe sme premerali so všetkými
  vylepšeniami: všetky varianty po poplatkoch prerábajú (PF 0,91-0,99,
  prepady 28-61 %), preto ostáva 4h.
- **Take-profit už nestrihá boom**: TP leží na 20R (predtým 6R) a obchod
  ukončuje trh - trailing, ochrana zisku, objem, sentiment. Strop 6R by na
  dátach odkrojil tretinu celkového zisku (najlepší obchod +15,6R).
- **Objemový exit**: keď je pozícia v zisku aspoň 1R a objem na 4h grafe
  ostane 2 sviečky pod polovicou priemeru (hybná sila vyprchala), zisk sa
  uzamkne. Na 2 rokoch dát zdvihol profit factor mimo vzorky z 1,09 na
  1,17 a znížil prepad. Vypína sa v Settings.
- **Výber AI modelu** priamo v Settings: rozbaľovací zoznam s odhadom
  mesačnej ceny; predvolený je najlacnejší model poskytovateľa
  (Claude Haiku ~0,9 $/mes. namiesto Opus ~35 $/mes.).

- Vstup do pozície ide predvolene **limitným (maker) príkazom** presne na
  cene signálu: poplatok 0,02 % namiesto 0,05 % a žiadny sklz pri vstupe.
  Náklady na obchod klesli z 0,14 % na 0,09 % hodnoty pozície. Ak cena
  k limitu do jednej sviečky neprída, vstup prepadne (zapíše sa do logu).
  Na 2 rokoch dát to zdvihlo výnos stratégie mimo vzorky z ~5 % na ~7 %
  ročne pri rovnakom riziku. V Settings sa dá prepnúť späť na market.
- Otestované aj pyramídovanie (prikupovanie v zisku pri +1R/+2R): výnos
  skoro nezmenený, prepad ~2× väčší - nenasadené.

## 1.2 (november 2026) - Kraken Futures: short, páka, viac mincí

Prečo zmena burzy: Binance od 1. 7. 2026 obmedzil služby pre účty z EÚ
(koniec prechodného obdobia MiCA). Kraken Futures je v EÚ regulovaný
(MiFID), funguje pre slovenské účty a má rovnaké poplatky ako Binance
Futures (maker 0,02 %, taker 0,05 %).

**Nové**
- Obchoduje sa na perpetual futures: **LONG aj SHORT**, bot zarába aj na
  poklese ceny. Spot ostáva na výber v Settings.
- **Páka**: predvolene 3×, nastaviteľná do 10× (strop pre EÚ retail).
  Páka nemení riziko obchodu - znižuje iba sumu zablokovanú v pozícii.
  Pri páke nad 5× appka výrazne varuje a žiada potvrdenie.
- Izolovaný margin, odhad likvidačnej ceny pri každej pozícii a poistka:
  obchod sa neotvorí, ak by stop-loss nebol výrazne pred likvidáciou.
- **Funding** (hodinový poplatok za držanie futures) sa simuluje v paper
  režime a započítava do výsledku obchodu aj do testov stratégií.
- **Automatický výber mincí podľa likvidity**: najobchodovanejšie kontrakty
  podľa 24h objemu v USD (meme mince a tokenizované akcie sa vynechávajú).
  Vlastný zoznam ostáva v Settings.
- **Objem obchodovania pri každej minci** v USD aj v minci: panel Trh na
  dashboarde, pruh pod grafom (objem, funding, open interest, mark) a
  karta Trh v mobilnej appke.
- **Nové stratégie na 4h grafe** (trend_rider_4h, donchian_4h), overené na
  2 rokoch dát Kraken Futures po všetkých poplatkoch: in-sample profit
  factor 1,34, out-of-sample 1,07 pri polovičnom prepade oproti 5m
  stratégiám. 5m stratégie sa na futures nepoužívajú (po poplatkoch
  prerábali). Obchodov bude málo - typicky jednotky za týždeň.
- Kurzy bežia cez WebSocket Kraken Futures (so záložným REST).
- Mobil: **potiahnutím nadol sa appka aktualizuje** a potvrdí
  „Aktualizované"; pozície ukazujú páku a likvidačnú cenu; karta Trh.
- Desktop zladený s pôvodným dizajnovým návrhom: ikony v navigácii,
  karty Win rate / Profit factor / Max drawdown, System panel.
- Risk: limit pozícií v rovnakom smere (krypto sa hýbe spolu).

**Dôležité**: prvá kalibrácia po inštalácii stiahne rok 1h dát pre každú
mincu a prebehne na pozadí (môže bežať aj pol hodiny). Bot medzitým beží.

## 1.1 – aktualizácia párovania mobilu

- Inštalátor rozpozná nainštalovaný ARGUS a ponúkne **Preinštalovať načisto**
  alebo **Iba aktualizovať** - netreba odinštalovať. Bežiaci ARGUS sám zatvorí.
  Dáta v `%APPDATA%\ARGUS` ostávajú.
- Graf: po uzavretí sviečky sa už neresetuje priblíženie ani posun.
- Ikony namiesto emoji/symbolových znakov v mobilnej appke aj na desktope.

- Mobilná appka má tlačidlo **Naskenovať QR kód** (kamera priamo v appke), takže sa
  spáruje appka z plochy, nie iba prehliadač. Po spárovaní ukáže potvrdenie.
- Desktop v karte Mobilná aplikácia živo ukazuje stav spojenia so serverom a
  pripojené telefóny (typ, appka/prehliadač, kedy boli naposledy aktívne) a po
  spárovaní zobrazí **Telefón spárovaný**. Použitý 6-miestny kód sa označí.
- Oprava: stav synchronizácie ostával „starting“, kým sa stránka neotvorila znova.

## 1.1 (november 2026)

Aktualizácia: stiahni nový `ARGUS-setup.exe` a nainštaluj cez starú verziu.
Dáta, obchody, modely a nastavenia v `%APPDATA%\ARGUS` ostávajú.
Databáza sa aktualizuje automaticky pri prvom spustení.

**Nové**
- Graf: aktuálna sviečka sa hýbe naživo (cena každé ~2 s z Kraken), farba ceny pri zmene.
- Poplatky: overené poplatky Binance a Kraken; každý obchod sa vyhodnocuje a
  dimenzuje **po odpočítaní poplatkov a sklzu**. Obchod, ktorého cieľ nepokryje
  poplatky, sa neotvorí. Na spotovom trhu iba LONG.
- Nastavenia priamo v aplikácii: rozpočet bota, max. suma na pozíciu, riziko,
  limity, burza, adaptívne výstupy, mince, AI.
- Otvorené pozície ukazujú vloženú sumu; tlačidlo **Zatvoriť** pri každej pozícii
  (desktop aj mobil). Ručné zatvorenia sa nepočítajú do učenia.
- Adaptívne riadenie pozícií: zníženie rizika, break-even vrátane poplatkov,
  trailing, ochrana zisku pred vrátením, reakcia na zvrat sentimentu a trhového
  režimu, posun take-profitu v silnom trende.
- Mince BTC, ETH, SOL, XRP; každá minca má vlastný výber stratégií
  (kalibrácia backtestom po poplatkoch + vlastné obchody).
- ML: tlačidlá Trénovať / Aktivovať / Vrátiť, automatické nasadenie lepšieho
  validovaného modelu, prvá kalibrácia hneď po prvom spustení; export a import
  všetkého naučeného do jedného súboru.
- AI analýza správ: Claude, GPT alebo Gemini (vždy jeden aktívny), kľúče
  šifrované (Windows DPAPI), denný rozpočet a prehľad skutočnej spotreby.
- Mobilná aplikácia: automatické nasadenie na Cloudflare, spárovanie QR kódom
  alebo 6-miestnym kódom, zatváranie pozícií z mobilu, notifikácie
  (návod: docs/MOBIL-NAVOD.md).

**Opravené**
- Prázdna equity krivka (jedna chybná časť stránky blokovala ostatné).
- Po reštarte aplikácie paper broker „zabudol“ otvorené pozície → nefungoval
  SL/TP ani ručné zatvorenie.
- Pri vysokých poplatkoch sa break-even stop mohol posunúť nad trhovú cenu.
- Simulovaný stop-loss sa teraz vyplní za reálnu trhovú cenu (aj pri skoku ceny).
- Pauza a EMERGENCY STOP platia aj po reštarte počítača.

## 1.0 (november 2026)
- Prvá inštalovateľná verzia pre Windows (oprava bieleho okna bez konzoly).
