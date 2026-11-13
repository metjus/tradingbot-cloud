# FÁZA 5 – Machine Learning (Adaptive Learning Engine)

Dátum: 2026-11-13

## Komponenty

| Modul | Účel |
|---|---|
| `argus/backtest/history.py` | Sťahovanie 5m histórie (Bitstamp public API, stránkovane, cache v SQLite) + resampling 5m→15m/1h |
| `argus/backtest/engine.py` | Backtest jadro: prehráva históriu cez reálny stack stratégií/decision engine; zbiera všetky signály s outcome TP-pred-SL (ML dataset) aj sekvenčnú equity simuláciu s nákladmi (metriky) |
| `argus/backtest/metrics.py` | Win rate, profit factor, expectancy, max drawdown, avg win/loss, per-strategy rozpad |
| `argus/ml/dataset.py` | Vektorizácia signálov – iba scale-free príznaky (RSI, ADX, ATR %, vzdialenosti od EMA/VWAP v ATR, pozícia v BB, objemový pomer, hodina dňa, smer, RR, one-hot stratégia a režim); verziovaná schéma stĺpcov |
| `argus/ml/model.py` | SetupScorer – P(TP pred SL), HistGradientBoosting (rovnaká rodina ako XGBoost/LightGBM, bez natívnych závislostí; rozhranie umožňuje neskoršiu výmenu) |
| `argus/ml/training.py` | Walk-forward validácia: chronologické foldy s 8h embargom proti úniku outcome cez hranicu, brány (min. vzorka 300, priem. AUC ≥ 0.55, žiadny fold < 0.5), top-decile win rate |
| `argus/ml/registry.py` | Verziovaný registry: candidate → validated/rejected → active → archived; aktivácia a rollback ako vedomé kroky; audit každej zmeny |
| `argus/analysis/selector.py` | Adaptívny výber stratégií: per-stratégia/per-režim štatistiky v R-násobkoch; PF < 1.0 pri n ≥ 30 ⇒ automatické vypnutie stratégie, opätovné zapnutie len cez nové prejdené štatistiky |

## Integrácia do Decision Engine

- Aktívny model pridáva faktor `ml_probability` = (p − 0.5) × 40 → −20..+20 bodov skóre.
- Bez modelu (alebo pri chybe modelu) je faktor 0 — engine je plne funkčný bez ML (požiadavka §21).
- Vypnuté stratégie selektor odfiltruje ešte pred vyhodnotením.

## Ochrany proti preučeniu (§9D)

1. Tréningové dáta vždy časovo predchádzajú validačným (walk-forward, rastúce okno).
2. 8-hodinové embargo medzi tréningom a validáciou (dlhšie než max. držanie pozície 96×5m) – žiadny outcome leakage.
3. Outcome simulácia: pri zásahu SL aj TP v tej istej sviečke sa počíta SL (pesimisticky).
4. Dataset tvoria VŠETKY signály (aj pod prahom), nie len vstúpené obchody – žiadny threshold bias.
5. Kandidát sa nikdy neaktivuje automaticky; šum musí byť zamietnutý (overené testom `test_walk_forward_rejects_noise`).
6. Len scale-free príznaky – žiadne absolútne ceny, model prenosný medzi symbolmi.

## Použitie

```bash
python -m argus.ml --symbols "BTC/USD,ETH/USD" --days 60
python -m argus.ml --list | --activate N | --rollback
```

Výsledky posledného behu: pozri report vo vlákne / audit log (`/api/models`, `/api/audit`).
