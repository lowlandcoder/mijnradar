#!/usr/bin/env python3
"""Hoofdscript voor mijnradar.lab023.nl.

Draait elke 5 minuten (via systemd-timer) en doet het volgende:
1. Historie: haalt ontbrekende RTCOR-bestanden van de afgelopen 2 uur op
   en rendert per tijdstap een PNG (schuivend venster van 25 frames).
2. Verwachting: haalt het nieuwste nowcastbestand op en rendert de
   25 verwachtingsframes.
3. Schrijft frames.json met alle beschikbare frames en de kaartgrenzen.
4. Ruimt oude bestanden op.

Gebruik:
  KNMI_API_KEY=... python3 -m knmi_radar.run --data /var/www/mijnradar/data
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import shutil
import sys

from knmi_radar.fetch import KNMIClient
from knmi_radar.render import render_bestand, grenzen

log = logging.getLogger("mijnradar")

HISTORIE = {"dataset": "nl_rdr_data_rtcor_5m", "versie": "1.0"}
NOWCAST = {"dataset": "radar_forecast", "versie": "2.0"}
VENSTER_MINUTEN = 120  # 2 uur historie
STAP_MINUTEN = 5


def tijd_uit_naam(bestandsnaam: str) -> dt.datetime | None:
    """Haalt het tijdstip (UTC) uit een KNMI-bestandsnaam met 12 cijfers."""
    m = re.search(r"(\d{12})", bestandsnaam)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(
        tzinfo=dt.timezone.utc
    )


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def verwerk_historie(client: KNMIClient, data_map: str, werk_map: str,
                     cache_map: str) -> list[dict]:
    """Zorgt dat voor elk 5-minutentijdstip in het venster een PNG bestaat."""
    uitvoer = os.path.join(data_map, "history")
    os.makedirs(uitvoer, exist_ok=True)
    aantal = VENSTER_MINUTEN // STAP_MINUTEN + 1
    bestanden = client.lijst_recent(**HISTORIE, aantal=aantal + 3)
    frames = []
    ondergrens = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        minutes=VENSTER_MINUTEN + 20
    )
    gewenst: list[tuple[dt.datetime, str]] = []
    for info in bestanden:
        t = tijd_uit_naam(info["filename"])
        if t and t >= ondergrens:
            gewenst.append((t, info["filename"]))
    gewenst.sort()
    gewenst = gewenst[-aantal:]
    for t, bestandsnaam in gewenst:
        png_naam = f"rt_{t.strftime('%Y%m%d%H%M')}.png"
        png_pad = os.path.join(uitvoer, png_naam)
        if not os.path.exists(png_pad):
            h5_pad = os.path.join(werk_map, bestandsnaam)
            client.download(**HISTORIE, bestandsnaam=bestandsnaam, doelpad=h5_pad)
            gerenderd = render_bestand(h5_pad, uitvoer, cache_map, prefix="tmp_rt")
            os.replace(os.path.join(uitvoer, gerenderd[0]["png"]), png_pad)
            os.remove(h5_pad)
        frames.append({"time": iso(t), "file": f"history/{png_naam}"})
    # Opruimen: PNG's buiten het venster verwijderen
    geldig = {f["file"].split("/")[-1] for f in frames}
    for naam in os.listdir(uitvoer):
        if naam.startswith("rt_") and naam not in geldig:
            os.remove(os.path.join(uitvoer, naam))
    return frames


def verwerk_nowcast(client: KNMIClient, data_map: str, werk_map: str,
                    cache_map: str, status: dict) -> list[dict]:
    """Rendert de 25 verwachtingsframes uit het nieuwste nowcastbestand."""
    uitvoer = os.path.join(data_map, "forecast")
    os.makedirs(uitvoer, exist_ok=True)
    recent = client.lijst_recent(**NOWCAST, aantal=1)
    if not recent:
        raise RuntimeError("Geen nowcastbestanden gevonden")
    bestandsnaam = recent[0]["filename"]
    starttijd = tijd_uit_naam(bestandsnaam)
    if status.get("laatste_nowcast") != bestandsnaam:
        h5_pad = os.path.join(werk_map, bestandsnaam)
        client.download(**NOWCAST, bestandsnaam=bestandsnaam, doelpad=h5_pad)
        # Eerst naar een tijdelijke map renderen, dan atomair wisselen
        tmp_map = uitvoer + ".nieuw"
        shutil.rmtree(tmp_map, ignore_errors=True)
        os.makedirs(tmp_map)
        render_bestand(h5_pad, tmp_map, cache_map, prefix="fc")
        os.remove(h5_pad)
        oud = uitvoer + ".oud"
        shutil.rmtree(oud, ignore_errors=True)
        os.replace(uitvoer, oud)
        os.replace(tmp_map, uitvoer)
        shutil.rmtree(oud, ignore_errors=True)
        status["laatste_nowcast"] = bestandsnaam
    frames = []
    pngs = sorted(n for n in os.listdir(uitvoer) if n.endswith(".png"))
    for i, naam in enumerate(pngs):
        t = starttijd + dt.timedelta(minutes=i * STAP_MINUTEN) if starttijd else None
        frames.append({"time": iso(t) if t else None, "file": f"forecast/{naam}"})
    return frames


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True,
                        help="Uitvoermap die nginx serveert, bijv. /var/www/mijnradar/data")
    parser.add_argument("--werk", default="/var/lib/mijnradar/werk",
                        help="Werkmap voor tijdelijke HDF5-bestanden")
    parser.add_argument("--cache", default="/var/lib/mijnradar/cache",
                        help="Cachemap voor de opzoektabel")
    argumenten = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    for map_ in (argumenten.data, argumenten.werk, argumenten.cache):
        os.makedirs(map_, exist_ok=True)
    os.makedirs(os.path.join(argumenten.data, "forecast"), exist_ok=True)

    status_pad = os.path.join(argumenten.cache, "status.json")
    status = {}
    if os.path.exists(status_pad):
        with open(status_pad) as f:
            status = json.load(f)

    client = KNMIClient()
    fouten = []
    historie_frames, nowcast_frames = [], []

    try:
        historie_frames = verwerk_historie(
            client, argumenten.data, argumenten.werk, argumenten.cache)
    except Exception as fout:  # noqa: BLE001
        log.error("Historie mislukt: %s", fout)
        fouten.append(f"historie: {fout}")

    try:
        nowcast_frames = verwerk_nowcast(
            client, argumenten.data, argumenten.werk, argumenten.cache, status)
    except Exception as fout:  # noqa: BLE001
        log.error("Nowcast mislukt: %s", fout)
        fouten.append(f"nowcast: {fout}")

    frames = {
        "generated": iso(dt.datetime.now(dt.timezone.utc)),
        "bounds": grenzen(),
        "history": historie_frames,
        "forecast": nowcast_frames,
        "errors": fouten,
    }
    frames_pad = os.path.join(argumenten.data, "frames.json")
    tmp = frames_pad + ".part"
    with open(tmp, "w") as f:
        json.dump(frames, f)
    os.replace(tmp, frames_pad)

    with open(status_pad, "w") as f:
        json.dump(status, f)

    log.info("Klaar: %d historieframes, %d verwachtingsframes, %d fouten",
             len(historie_frames), len(nowcast_frames), len(fouten))
    return 1 if fouten and not (historie_frames or nowcast_frames) else 0


if __name__ == "__main__":
    sys.exit(main())
