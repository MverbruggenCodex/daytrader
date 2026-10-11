---
description: Openstaande paper-orders van de daytrader bekijken en na bevestiging op het Liquid paper account zetten
---

1. `git pull`, daarna `python -m trader orders`.
2. Liquid `get_portfolio` → `inbox/portfolio.json` → `python -m trader liquid-sync --file inbox/portfolio.json`.
   Geen paper modus (exitcode 2)? Stop en meld het.
3. Maak voor elke order in `propose` een vers `suggest_trade`-voorstel (zie `trader-run.md`, stap 5) en voor `close`
   een `close_position`-voorstel. Controleer `executionMode == "paper"`.
4. Toon alle voorstellen en vraag de gebruiker per order om bevestiging. Voer pas na een expliciete "ja" voor die
   order `execute_order` / `close_position` (confirmed) uit, met ongewijzigde termen en de `idempotency_key`.
5. Leg uitkomsten vast met `python -m trader order-update`, run `python -m trader export`, werk het dashboard bij
   (`trader-run.md` stap 6) en commit + push `state/`.
