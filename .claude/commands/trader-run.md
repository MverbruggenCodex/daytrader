---
description: Eén volledige ronde van de paper trader - candles ophalen, engine draaien, dashboard bijwerken, state committen
---

Voer één ronde uit van de ORB + supply & demand paper trader. Plaats nooit echte orders.

1. **Plan.** Run `python -m trader fetch-plan`. Je krijgt per markt `symbol`, `limit` en `inbox_file`.

2. **Candles.** Roep per markt de Liquid-connector aan: `show_chart(symbol=<symbol>, interval="5m", limit=<limit>)`.
   Schrijf het resultaat compact naar `inbox_file` als `{"candles": [[timestamp, open, high, low, close], ...]}`
   (getallen, geen strings). Lukt Liquid niet, sla die markt dan over en meld het aan het eind.
   Heeft deze machine internet naar api.hyperliquid.xyz, dan mag je stap 2 overslaan en in stap 4
   `--source hyperliquid` gebruiken.

3. **Nieuwsfilter.** Gebruik Liquid `get_news` (en zo nodig web search naar de economische kalender van vandaag).
   Staat er binnen 60 minuten een high-impact release (FOMC/rente, CPI, NFP/banen, BBP, ECB)? Voeg dan een blackout toe
   van 15 min ervoor tot 30 min erna:
   `python -m trader blackout --start 2026-10-14T14:15 --end 2026-10-14T15:00 --reason "CPI VS"`
   (tijden in Amsterdamse tijd; `--markets EURUSD,XAUUSD` om te beperken, standaard alle markten).
   Is er opvallend nieuws voor een markt, schrijf dan één korte notitie:
   `python -m trader note --market BTCUSD --title "..." --text "..." --trigger Nieuws`.

4. **Engine.** Run `python -m trader run --source inbox`. Lees `new_events` en `summary` uit de output.

5. **Dashboard.** De dashboard-URL staat in `config.json` (`dashboard_url`). Upload elk bestand in `export/`
   (`account.json`, `trades.json`, `analyses.json`, `markets.json`, `zones.json`) met de Artifact-tool
   (`asset: true`, `url` = dashboard) en werk daarna in één `ArtifactData` batch de datasets `account`, `trades`,
   `analyses`, `markets` en `zones` bij: `source: {kind: "file", url: <nieuwe /_blob/... url>, name: <bestandsnaam>}`
   plus `updated: {at: <nu ISO>, by: "Claude"}` (lees de datasets eerst voor hun `version`).
   De BTC-grafiek en live koers op het dashboard komen rechtstreeks van Liquid en hoeven niet bijgewerkt.

6. **Opslaan.** `git add state/ && git commit -m "Run <tijd>: <korte samenvatting>" && git push`.

7. **Rapport.** Eén korte alinea: nieuwe trades of exits (met resultaat), actieve zones, saldo en open P/L,
   en wat er mislukte. Geen herhaling van alle stappen.
