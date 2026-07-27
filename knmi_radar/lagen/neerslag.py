"""Neerslaglaag: KNMI-radarcomposiet en nowcast.

Bron: HDF5-bestanden uit de KNMI Open Data API, in een polair stereografisch
raster. Die worden met de opzoektabel uit `raster` omgezet naar webmercator en
met de kleurschaal hieronder ingekleurd. Droog en 'geen data' zijn doorzichtig.

Twee reeksen:
  history   een schuivend venster van 2 uur, elke 5 minuten een los bestand
  forecast  25 verwachtingsframes uit een enkel nowcastbestand
"""
import datetime as dt
import logging
import os
import re
import shutil

import h5py
import numpy as np
from PIL import Image

from knmi_radar import alert, kleur, raster
from knmi_radar.bronnen.opendata import KNMIClient
from knmi_radar.hulp import iso

log = logging.getLogger(__name__)

SLEUTEL = "neerslag"

HISTORIE = {"dataset": "nl_rdr_data_rtcor_5m", "versie": "1.0"}
NOWCAST = {"dataset": "radar_forecast", "versie": "2.0"}
VENSTER_MINUTEN = 120  # 2 uur historie
STAP_MINUTEN = 5

# Beschrijving van de laag. Komt zo in frames.json terecht, zodat de browser
# niets over de laag hoeft te weten.
BESCHRIJVING = {
    "naam": "Neerslag",
    "pictogram": "\U0001f327",
    "eenheid": "mm/uur",
    "stap": STAP_MINUTEN,
    "schaal": "log",
    "bron": "KNMI Open Data, radarcomposiet en nowcast",
}

# Kleurenschaal: neerslagintensiteit in mm/uur naar RGBA
KLEURSTOPS = [
    (0.1, (208, 240, 255, 90)),
    (0.3, (112, 200, 255, 150)),
    (1.0, (0, 102, 255, 190)),
    (3.0, (0, 204, 68, 210)),
    (10.0, (255, 238, 0, 225)),
    (30.0, (255, 102, 0, 240)),
    (100.0, (204, 0, 0, 255)),
]


def beschikbaar() -> bool:
    """De laag draait alleen mee als er een sleutel voor de Open Data API is."""
    return bool(os.environ.get("KNMI_API_KEY"))


def legenda() -> list[list]:
    """Kleurstops van deze laag als [waarde, "#rrggbb"]."""
    return kleur.hex_stops(KLEURSTOPS)


def tijd_uit_naam(bestandsnaam: str) -> dt.datetime | None:
    """Haalt het tijdstip (UTC) uit een KNMI-bestandsnaam met 12 cijfers."""
    m = re.search(r"(\d{12})", bestandsnaam)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(
        tzinfo=dt.timezone.utc)


# ── Renderen ─────────────────────────────────────────────────────────────────

def render_bestand(h5_pad: str, uitvoer_map: str, cache_map: str,
                   prefix: str = "frame") -> list[dict]:
    """Rendert alle beeldgroepen in een HDF5-bestand naar PNG's.

    Geeft een lijst terug met per frame de PNG-bestandsnaam en, indien
    beschikbaar, het geldigheidstijdstip uit het bestand.
    """
    resultaat = []
    with h5py.File(h5_pad, "r") as h5:
        proj4, kol_offset, rij_offset = raster.lees_projectie(h5)
        groepen = sorted(
            (g for g in h5.keys() if re.fullmatch(r"image\d+", g)),
            key=lambda g: int(g[5:]),
        )
        if not groepen:
            raise ValueError(f"Geen beeldgroepen gevonden in {h5_pad}")
        tabel = None
        for volgnummer, naam in enumerate(groepen):
            groep = h5[naam]
            data = np.asarray(groep["image_data"])
            if tabel is None:
                tabel = raster.opzoektabel(proj4, kol_offset, rij_offset,
                                           data.shape, cache_map)
            a, b, nodata = raster.kalibratie(groep)
            doel = raster.toepassen(tabel, data, nodata)
            # 0,01 mm per 5 min -> mm/uur; nodata wordt 0 (doorzichtig)
            mm_per_uur = np.where(doel == nodata, 0.0, (doel * a + b) * 12.0)
            beeld = kleur.naar_rgba(mm_per_uur, KLEURSTOPS, log=True)
            png_naam = f"{prefix}_{volgnummer:02d}.png"
            Image.fromarray(beeld, "RGBA").save(os.path.join(uitvoer_map, png_naam),
                                                optimize=True)
            tijdstip = None
            ruw = groep.attrs.get("image_datetime_valid")
            if ruw is not None:
                ruw = np.ravel(ruw)[0]
                tijdstip = ruw.decode() if isinstance(ruw, bytes) else str(ruw)
            resultaat.append({"png": png_naam, "tijd_attr": tijdstip})
    return resultaat


# ── Reeksen bijwerken ────────────────────────────────────────────────────────

def _historie(client: KNMIClient, data_map: str, werk_map: str,
              cache_map: str) -> list[dict]:
    """Zorgt dat voor elk 5-minutentijdstip in het venster een PNG bestaat."""
    uitvoer = os.path.join(data_map, "history")
    os.makedirs(uitvoer, exist_ok=True)
    aantal = VENSTER_MINUTEN // STAP_MINUTEN + 1
    bestanden = client.lijst_recent(**HISTORIE, aantal=aantal + 3)
    frames = []
    ondergrens = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        minutes=VENSTER_MINUTEN + 20)
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


def _nowcast(client: KNMIClient, data_map: str, werk_map: str,
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
        # Weeralert: controleer de nieuwe verwachting op neerslag rond het
        # ingestelde punt. Een fout hier mag het renderen niet breken.
        try:
            alert.controleer(h5_pad, starttijd, status)
        except Exception as fout:  # noqa: BLE001
            log.error("Weeralert mislukt: %s", fout)
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


def verwerk(data_map: str, werk_map: str, cache_map: str, status: dict) -> dict:
    """Werkt beide reeksen bij. Een fout in de ene breekt de andere niet."""
    client = KNMIClient()
    historie, verwachting, fouten = [], [], []
    try:
        historie = _historie(client, data_map, werk_map, cache_map)
    except Exception as fout:  # noqa: BLE001
        log.error("Historie mislukt: %s", fout)
        fouten.append("historie: %s" % fout)
    try:
        verwachting = _nowcast(client, data_map, werk_map, cache_map, status)
    except Exception as fout:  # noqa: BLE001
        log.error("Nowcast mislukt: %s", fout)
        fouten.append("nowcast: %s" % fout)
    return {"history": historie, "forecast": verwachting, "fouten": fouten}
