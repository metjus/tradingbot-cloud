# Zoznam zmien

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
