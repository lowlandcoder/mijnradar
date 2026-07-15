# MijnRadar

Neerslagradar voor Nederland: historie van de laatste 2 uur en een korte-
termijnverwachting, op een Leaflet-kaart in de gedeelde huisstijl (nog niet
volledig doorgevoerd, zie Aandachtspunten in OVERZICHT.md).

## Wat de pagina toont

- Kaart met neerslagintensiteit, als schuivende reeks PNG-frames: 25 frames
  historie (laatste 2 uur, elke 5 minuten) en 25 frames verwachting
  (nowcast).
- De browser leest de kaartgrenzen en beschikbare frames uit `frames.json`.

## Techniek

- **Webpagina:** `index.html`, rechtstreeks vanuit de browser, geen
  buildstap.
- **Backend:** Python-module `knmi_radar/`, gedraaid als systemd-dienst
  (`mijnradar.service`) met een timer die elke 5 minuten ververst
  (`mijnradar.timer`). Haalt HDF5-radarbestanden op via de KNMI Open Data
  API, rendert ze naar transparante PNG's (`knmi_radar/render.py`) en
  schrijft `frames.json` (`knmi_radar/run.py`).
- **Projectie:** het KNMI-raster (polaire stereografisch, km) wordt
  omgerekend naar webmercator via een eenmalig berekende opzoektabel, die op
  schijf wordt bewaard (cache) om niet bij elke run opnieuw te hoeven
  rekenen.
- **Databron:** KNMI Open Data API, datasets `nl_rdr_data_rtcor_5m`
  (historie) en `radar_forecast` (nowcast).

## Instellingen

Kopieer `mijnradar.env.example` naar `/etc/mijnradar/mijnradar.env` op de
server en vul `KNMI_API_KEY` in. Dit bestand bevat een geheim en hoort nooit
op GitHub.

## Serveronderdelen

- Code en virtualenv: `/opt/mijnradar/` (module `knmi_radar/`,
  `requirements.txt`).
- Instellingen: `/etc/mijnradar/mijnradar.env`.
- Werkmap (tijdelijke HDF5-bestanden): `/var/lib/mijnradar/werk/`.
- Cache (opzoektabel voor de projectie-omzetting): `/var/lib/mijnradar/cache/`.
- Uitvoer voor de webpagina: `/var/www/mijnradar/data/` (`frames.json`,
  `history/`, `forecast/`).
- Systemd-eenheden: `mijnradar.service` en `mijnradar.timer`, geïnstalleerd
  in `/etc/systemd/system/` (bronbestanden in dit repository onder
  `systemd/`).

Er is nog geen publicatiescript; bijwerken op de server gebeurt vooralsnog
handmatig (bestanden kopiëren naar `/opt/mijnradar/` en de dienst herstarten
met `sudo systemctl restart mijnradar.service`).

## Bekende storing en oplossing (2026-07-15)

De cachesleutel voor de opzoektabel werd berekend met Pythons ingebouwde
`hash()` op een tupel dat ook tekst bevatte. Tekst-hashing is sinds Python
3.3 per processtart willekeurig gezouten (beveiligingsmaatregel); omdat de
dienst elke 5 minuten als nieuw proces start, kreeg de tabel telkens een
andere bestandsnaam en werd de bestaande cache nooit herkend. Gevolg: bij
elke run een nieuw bestand van circa 4,4 MB, oplopend tot 7,3 GB in 8 dagen
(circa 900 MB/dag), met een bijna volgelopen schijf als gevolg.

Opgelost in `knmi_radar/render.py` door de sleutel te berekenen met
`hashlib.sha256` in plaats van `hash()`. Die is wel stabiel tussen
processen. Na de wijziging is bevestigd dat een herhaalde run dezelfde
opzoektabel hergebruikt (geen nieuw bestand, geen herberekening). De 1668
verouderde cachebestanden zijn daarna verwijderd.
