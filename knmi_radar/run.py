#!/usr/bin/env python3
"""Hoofdscript voor mijnradar.lab023.nl.

Draait elke 5 minuten (via systemd-timer) en werkt alle ingeschakelde lagen
bij. Wat een laag precies doet, weet de laag zelf; dit script kent alleen de
afspraak uit knmi_radar/lagen/__init__.py.

Per run:
1. Voor elke beschikbare laag de ontbrekende PNG's renderen.
2. frames.json schrijven met per laag de beschrijving, de legenda en de
   reeksen `history` en `forecast`.
3. De sleutels `history` en `forecast` op het hoogste niveau blijven wijzen
   naar de standaardlaag, zodat een oudere pagina blijft werken.

Een laag die geen API-sleutel heeft wordt overgeslagen, niet als fout geteld.
Zo kan de code worden uitgerold voordat een sleutel is ingesteld.

Gebruik:
  KNMI_API_KEY=... KNMI_WMS_API_KEY=... \
      python3 -m knmi_radar.run --data /var/www/mijnradar/data
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys

from knmi_radar.hulp import iso
from knmi_radar.lagen import ALLE
from knmi_radar.raster import grenzen

log = logging.getLogger("mijnradar")

STANDAARDLAAG = "neerslag"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True,
                        help="Uitvoermap die nginx serveert, bijv. /var/www/mijnradar/data")
    parser.add_argument("--werk", default="/var/lib/mijnradar/werk",
                        help="Werkmap voor tijdelijke brondbestanden")
    parser.add_argument("--cache", default="/var/lib/mijnradar/cache",
                        help="Cachemap voor de opzoektabel")
    parser.add_argument("--lagen", default="",
                        help="Alleen deze lagen bijwerken, gescheiden door komma's")
    argumenten = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    for map_ in (argumenten.data, argumenten.werk, argumenten.cache):
        os.makedirs(map_, exist_ok=True)

    gekozen = {s.strip() for s in argumenten.lagen.split(",") if s.strip()}
    onbekend = gekozen - set(ALLE)
    if onbekend:
        print("Onbekende laag: %s. Bekend: %s"
              % (", ".join(sorted(onbekend)), ", ".join(ALLE)), file=sys.stderr)
        return 2

    status_pad = os.path.join(argumenten.cache, "status.json")
    status = {}
    if os.path.exists(status_pad):
        with open(status_pad) as f:
            status = json.load(f)

    lagen, fouten = {}, []
    for sleutel, module in ALLE.items():
        if gekozen and sleutel not in gekozen:
            continue
        if not module.beschikbaar():
            log.info("Laag %s overgeslagen: geen sleutel ingesteld", sleutel)
            continue
        try:
            uitkomst = module.verwerk(argumenten.data, argumenten.werk,
                                      argumenten.cache, status)
        except Exception as fout:  # noqa: BLE001
            log.error("Laag %s mislukt: %s", sleutel, fout)
            fouten.append(f"{sleutel}: {fout}")
            continue
        fouten.extend(f"{sleutel}: {f}" for f in uitkomst.get("fouten", []))
        beschrijving = dict(module.BESCHRIJVING)
        beschrijving["legenda"] = module.legenda()
        beschrijving["history"] = uitkomst.get("history", [])
        beschrijving["forecast"] = uitkomst.get("forecast", [])
        lagen[sleutel] = beschrijving

    standaard = STANDAARDLAAG if STANDAARDLAAG in lagen else next(iter(lagen), None)
    hoofd = lagen.get(standaard, {})

    # De sleutel van de CARTO-basiskaart gaat mee naar de browser, zodat die
    # in /etc/mijnradar/mijnradar.env kan blijven staan en niet in de repo.
    # Blijft de sleutel leeg, dan toont de basiskaart een watermerk maar werkt
    # de pagina verder gewoon.
    frames = {
        "generated": iso(dt.datetime.now(dt.timezone.utc)),
        "bounds": grenzen(),
        "standaardlaag": standaard,
        "basiskaart": {"sleutel": os.environ.get("CARTO_KEY", "").strip()},
        "layers": lagen,
        # Overgangsregeling: de oude sleutels blijven voorlopig staan, zodat een
        # pagina die nog niet laagbewust is gewoon blijft werken.
        "history": hoofd.get("history", []),
        "forecast": hoofd.get("forecast", []),
        "errors": fouten,
    }
    frames_pad = os.path.join(argumenten.data, "frames.json")
    tmp = frames_pad + ".part"
    with open(tmp, "w") as f:
        json.dump(frames, f)
    os.replace(tmp, frames_pad)

    with open(status_pad, "w") as f:
        json.dump(status, f)

    totaal = 0
    for sleutel, laag in lagen.items():
        log.info("Laag %s: %d historieframes, %d verwachtingsframes",
                 sleutel, len(laag["history"]), len(laag["forecast"]))
        totaal += len(laag["history"]) + len(laag["forecast"])
    log.info("Klaar: %d lagen, %d frames, %d fouten",
             len(lagen), totaal, len(fouten))
    return 1 if fouten and not totaal else 0


if __name__ == "__main__":
    sys.exit(main())
