# Prosper – Zimmo-scan

Dagelijkse scan van Zimmo voor de zone in `config.json`.
Resultaat: `data/latest.json` (+ gedateerde kopie per dag).

- Handmatig starten: tab **Actions** → *Prosper Zimmo-scan* → **Run workflow** (vink *debug* aan voor ruwe HTML in `debug/`).
- Automatisch: elke dag rond 06:15 Belgische tijd.
- Zone aanpassen: `config.json` bewerken.
