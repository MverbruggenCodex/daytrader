# Daytrader — ORB + Trade with Pat supply & demand (paper trading)

Een fictieve daytrader met een startaccount van **€50**. De engine handelt op 5-minuten-candles van
Liquid/Hyperliquid volgens vaste regels en houdt alles bij in een JSON-staat. Er worden **geen echte orders**
geplaatst. Het dashboard (Claude-artifact) toont account, trades, zones en het analyselogboek.

## Strategie

**Sessies (Amsterdamse tijd, zomer):** Londen opent 09:00, New York 15:30. De eerste 15 minuten vormen de
*openingsrange*. Entries tot 12:00 (Londen) en 18:00 (New York); posities gaan dicht bij het sessie-einde
(17:00 resp. 22:00). Tijden schuiven automatisch mee met zomer- en wintertijd.

**Zone (Pat, stap 1):** na de range zoekt de engine *displacement*: minimaal 3 candles op rij in één richting,
gemiddeld ≥ 1,5x zo groot als normaal, met een **fair value gap** en een **break of structure**. De zone is de laatste
tegengestelde candle vóór die beweging (wick tot wick; alleen de body als hij breder is dan 1,5 ATR).

**ORB-koppeling:** de displacement moet door de openingsrange breken, in de richting van de trend.

**Trend (Pat, stap 2):** 15-minuten-structuur: hogere toppen + hogere bodems = alleen long; lagere toppen + lagere bodems
= alleen short; zijwaarts = geen trade.

**Entry (Pat, stap 3, normaal model):** de koers tikt de zone aan en een candle sluit weer **buiten** de zone in de
tradingrichting → entry op die slotkoers. Stop net voorbij de zone (+0,1 ATR). Doel: de recente top/bodem van de beweging.
Minimaal **1:2** risk/reward, anders geen trade.

**Pats filters:**
- slot dóór de zone = dode zone;
- alleen verse zones (eerste aanraking);
- geen vallend mes (instap-candle groter dan 2 ATR);
- de diepste zone is de sterkste: ligt er binnen 2 ATR een diepere verse zone, dan wordt de hogere overgeslagen;
- discount en level stack worden per trade vastgelegd.

**Risico:** 3% van het saldo per trade, max 3 open trades, max 1 trade per markt per sessie, stoppen bij −6% op een dag.
Fees 0,045% per kant. Nieuwsfilter via `blackout`.

## Markten

| Markt | Symbool Liquid | Sessies |
|---|---|---|
| BTCUSD | `BTC` | Londen, New York (ook weekend) |
| EURUSD | `xyz:EUR` | Londen, New York |
| XAUUSD | `xyz:GOLD` | Londen, New York |
| SP500 | `xyz:SP500` | New York (vervangt DJ30) |
| USDCAD, DJ30 | — | niet beschikbaar op Liquid |

## Gebruik

Python 3.10+, geen externe packages nodig.

```bash
python -m trader status                      # saldo, open trades, actieve zones
python -m trader run --source hyperliquid    # één ronde met live data (machine met internet)
python -m trader run --source hyperliquid --loop   # lokaal 24/7, elke 5 minuten
python -m trader run --source inbox          # met candles die Claude via Liquid in inbox/ zette
python -m trader backtest --days 14          # backtest op Hyperliquid-data (lokaal)
python -m trader blackout --start 2026-10-14T14:15 --end 2026-10-14T15:00 --reason "CPI VS"
python tests/run_tests.py                    # tests
```

### In Claude Code
Gebruik `/trader-run` (zie `.claude/commands/trader-run.md`): Claude haalt candles op via de Liquid-connector,
draait de engine, werkt het dashboard bij en commit de staat. Een geplande taak draait deze ronde elk uur; omdat de
engine alle gemiste 5-minuten-candles alsnog in volgorde verwerkt, levert dat dezelfde trades op als realtime meekijken,
alleen later gemeld.

## Structuur

```
config.json          account, markten, sessies, strategie-instellingen
trader/structure.py  ATR, swing points, trend, fair value gaps
trader/zones.py      displacement, zones, Pats entry-regels
trader/sessions.py   sessievensters (tijdzone-bewust)
trader/broker.py     paper broker: sizing, stop/doel, fees, daglimiet
trader/engine.py     candle-voor-candle verwerking
trader/export.py     JSON voor het dashboard
state/state.json     staat (trades, zones, logboek, candle-cache)
```

> Dit is een experiment met nepgeld en geen beleggingsadvies. Resultaten uit backtests of paper trading
> zeggen weinig over echt handelen (slippage, spreads, minimale ordergroottes).
