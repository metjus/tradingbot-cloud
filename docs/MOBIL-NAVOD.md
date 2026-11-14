# Mobilná aplikácia ARGUS – podrobný návod

Mobilná aplikácia ti ukazuje stav bota kdekoľvek na svete: kapitál, zisk/stratu,
otvorené pozície, históriu, správy, a dovolí ti bota **spustiť, pozastaviť,
núdzovo zastaviť alebo zatvoriť konkrétnu pozíciu**. Posiela aj push notifikácie.

Obchoduje sa vždy na tvojom počítači. Mobil a server sú iba „okno“ a „diaľkové
ovládanie“ – nikdy nevidia API kľúče burzy a nikdy samy neobchodujú.

```
 Telefón (aplikácia)  ⇄  Tvoj Cloudflare server (zadarmo)  ⇄  Počítač s ARGUS
                         (spojenie vždy nadväzuje počítač – žiadne otvorené porty)
```

Celé nastavenie trvá asi 15 minút a robí sa iba raz.

---

## Krok 1 – Cloudflare údaje v GitHube (raz)

Server beží na tvojom bezplatnom Cloudflare účte. GitHub ho nasadí sám, potrebuje
len dva údaje.

**Najprv skontroluj, či ich už nemáš** (mal si ich pri starom projekte):
GitHub → repozitár **tradingbot-cloud** → **Settings** → **Secrets and variables** →
**Actions**. Ak tam vidíš `CLOUDFLARE_API_TOKEN` aj `CLOUDFLARE_ACCOUNT_ID`,
**preskoč na krok 2**.

Ak nie:

1. Prihlás sa na **dash.cloudflare.com** (účet je zadarmo).
2. **Account ID:** na úvodnej stránke účtu vpravo (alebo v adrese
   `dash.cloudflare.com/<ACCOUNT_ID>/...`) – skopíruj ho.
3. **API token:** vpravo hore ikona profilu → **My Profile** → **API Tokens** →
   **Create Token** → šablóna **Edit Cloudflare Workers** → **Use template**.
   V časti *Permissions* klikni **+ Add more** a pridaj
   *Account → D1 → Edit*. Potom **Continue to summary** → **Create Token** a token
   skopíruj (zobrazí sa iba raz).
4. V GitHube: **Settings → Secrets and variables → Actions → New repository
   secret**:
   - Name `CLOUDFLARE_API_TOKEN`, Secret = token z bodu 3
   - Name `CLOUDFLARE_ACCOUNT_ID`, Secret = Account ID z bodu 2

*Voliteľné (vyššia bezpečnosť):* pridaj aj secret `ARGUS_PAIRING_CODE` s
ľubovoľným heslom. Potom pôjde spárovať iba s týmto heslom.

## Krok 2 – Nasadenie servera (raz, potom automaticky)

1. GitHub → repozitár → záložka **Actions** → vľavo **Deploy mobile app
   (Cloudflare)** → vpravo **Run workflow** → **Run workflow**.
2. Po ~1–2 minútach sa beh zazelená. Klikni naň – v časti **Summary** je
   **Adresa servera**, napr. `https://argus-sync.tvoje-meno.workers.dev`.
   Skopíruj ju.

Pri každej ďalšej zmene mobilnej aplikácie sa server aktualizuje sám.

> Ak beh zčervená s hláškou „Chýbajú Cloudflare údaje“, vráť sa ku kroku 1.
> Ak hlási chybu oprávnení pri D1, tokenu chýba oprávnenie *D1 → Edit*.

## Krok 3 – Spárovanie s počítačom (raz)

1. Na počítači otvor ARGUS → vľavo **Settings** → hore karta **Mobilná aplikácia**.
2. Vlož **adresu servera** z kroku 2 (a heslo, ak si nastavil `ARGUS_PAIRING_CODE`).
3. Klikni **Spárovať**. Zobrazí sa QR kód a stav synchronizácie „ok“.

## Krok 4a – Android

1. Naskenuj QR kód fotoaparátom (alebo Google Lens) a otvor odkaz v Chrome.
   Aplikácia sa sama spáruje a ukáže tvoj kapitál.
2. Chrome ponúkne **Inštalovať aplikáciu** (alebo menu ⋮ → **Pridať na plochu**).
   Na ploche sa objaví ikona ARGUS.
3. V aplikácii otvor **Ovládanie** → vyber, ktoré notifikácie chceš →
   **Zapnúť / uložiť notifikácie** → povoľ.

## Krok 4b – iPhone (iOS 16.4 a novší)

Na iPhone fungujú notifikácie iba v aplikácii pridanej na plochu.

1. Otvor v **Safari** adresu servera z kroku 2 (alebo naskenuj QR kód).
2. Dole tlačidlo **Zdieľať** (štvorček so šípkou) → **Pridať na plochu** → **Pridať**.
3. Otvor ARGUS **z plochy**. Zobrazí sa obrazovka Spárovanie.
4. Na počítači v karte Mobilná aplikácia klikni **Zobraziť 6-miestny kód** a
   zadaj ho do telefónu (kód platí 10 minút) → **Spárovať**.
5. **Ovládanie** → vyber notifikácie → **Zapnúť / uložiť notifikácie** → **Povoliť**.

## Ako aplikácia funguje

| Obrazovka | Čo ukazuje |
|---|---|
| **Prehľad** | kapitál, denný a celkový zisk, koľko je vložené v pozíciách, krivka, otvorené pozície so SL/TP a aktuálnou cenou, posledné udalosti |
| **História** | uzatvorené obchody s výsledkom a dôvodom výstupu |
| **Správy** | sentiment pre každú mincu a posledné správy |
| **Ovládanie** | Spustiť, Pauza, EMERGENCY STOP, výber notifikácií, odpojenie telefónu |

- **ONLINE / OFFLINE** vpravo hore: či počítač s ARGUS posiela dáta. Pri OFFLINE
  vidíš posledný známy stav a čas poslednej aktualizácie – nikdy vymyslené dáta.
- **Príkazy** (Pauza, Stop, Zatvoriť pozíciu) vykoná počítač a aplikácia počká na
  jeho potvrdenie. Ak je počítač vypnutý, príkaz čaká najviac 5 minút, potom
  prepadne (aby sa neskôr nevykonal nečakane).
- **Pauza** = nové obchody sa neotvárajú, otvorené dobehnú. **EMERGENCY STOP** =
  všetky pozície sa zatvoria za trhovú cenu. Oba stavy platia aj po reštarte počítača.

## Riešenie problémov

| Problém | Riešenie |
|---|---|
| Spárovanie hlási „už spárovaný“ | Na pôvodnom počítači v karte Mobilná aplikácia klikni **Zrušiť párovanie**. Ak to nejde: Actions → Deploy mobile app → Run workflow so zaškrtnutým **Zrušiť párovanie**. |
| Telefón ukazuje „ZLÝ KÓD“ | Kód bol zmenený (tlačidlo Nový kód). Spáruj telefón znova 6-miestnym kódom. |
| Stále OFFLINE | Beží ARGUS na počítači? V karte Mobilná aplikácia musí byť Synchronizácia „ok“. |
| Neprichádzajú notifikácie (iPhone) | Aplikácia musí byť otvorená z plochy, nie zo Safari; v Nastavenia iPhonu → Notifikácie → ARGUS povolené. |
| Nevidím QR kód | Desktop potrebuje internet na načítanie generátora QR; použi 6-miestny kód. |

## Bezpečnosť

- Párovacie kódy sa na serveri ukladajú iba ako odtlačok (hash); počítač a telefón
  majú **odlišné** kódy – telefón nemôže posielať dáta za počítač.
- API kľúče búrz a AI sa nikdy neposielajú na server ani do telefónu.
- **Nový kód** v desktope okamžite odpojí všetky telefóny; **Zrušiť párovanie**
  vymaže všetko na serveri.
- Starý server z predchádzajúceho projektu (`tradingbot-cloud-paper`) s ARGUS
  nesúvisí – v Cloudflare ho môžeš zmazať: Workers & Pages → tradingbot-cloud-paper
  → Settings → Delete.
