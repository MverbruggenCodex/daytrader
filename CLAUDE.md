# Daytrader — instructies voor Claude

Fictieve (paper) daytrader met een account van €50. Strategie: **ORB + Trade with Pat supply & demand**
op 5-minuten-candles. Er worden **nooit echte orders** geplaatst — ook niet via de Liquid-connector,
die in live modus staat. Alle trades bestaan alleen in `state/state.json` en op het dashboard.

- Dashboard: zie `dashboard_url` in `config.json` (Claude-artifact van het type Dashboard).
- Markten: BTCUSD (`BTC`), EURUSD (`xyz:EUR`), XAUUSD (`xyz:GOLD`), SP500 (`xyz:SP500`, vervangt DJ30).
  USDCAD en DJ30 staan niet op Liquid/Hyperliquid en zijn uitgeschakeld.
- Regels staan in `README.md` en in code: `trader/zones.py` (zones + entry), `trader/engine.py` (sessies, filters).

## De cyclus (`/trader-run`)

Volg `.claude/commands/trader-run.md`. Kort:
1. `python -m trader fetch-plan` → per markt symbool en aantal candles.
2. Haal per markt candles op met de Liquid-connector (`show_chart`, interval `5m`, `limit` uit het plan) en
   schrijf ze compact naar `inbox/<MARKET>.json` als `{"candles": [[t, o, h, l, c], ...]}`.
   (Lokaal met internet kan ook `python -m trader run --source hyperliquid`, dan is stap 2 niet nodig.)
3. Nieuwsfilter: check groot macronieuws (rente, CPI, banen) in het komende uur en voeg zo nodig toe:
   `python -m trader blackout --start <ISO> --end <ISO> --reason "<wat>"`.
4. `python -m trader run --source inbox` → verwerkt alle nieuwe gesloten candles, schrijft `export/*.json`.
5. Werk het dashboard bij (zie command-bestand) en commit `state/` naar GitHub.

## Regels voor Claude
- Verander geen trades of uitkomsten met de hand; de engine is de enige die trades opent en sluit.
- Eigen context (nieuws, opvallende bewegingen) gaat via `python -m trader note ...` in het logboek.
- Tests: `python tests/run_tests.py` (werkt met en zonder pytest). Draai ze na elke codewijziging.
- Geen beleggingsadvies aan de gebruiker; dit is een experiment met nepgeld.
