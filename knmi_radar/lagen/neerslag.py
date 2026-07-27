"""Neerslaglaag: KNMI-radarcomposiet en nowcast.

Bron: HDF5-bestanden uit de KNMI Open Data API, in een polair stereografisch
raster. Die worden met de opzoektabel uit `raster` omgezet naar webmercator en
met de kleurschaal hieronder ingekleurd. Droog en 'geen data' zijn doorzichtig.
"""
import logging
import os
import re

import h5py
import numpy as np
from PIL import Image

from knmi_radar import kleur, raster

log = logging.getLogger(__name__)

# Beschrijving van de laag. Komt zo in frames.json terecht, zodat de browser
# niets over de laag hoeft te weten.
BESCHRIJVING = {
    "naam": "Neerslag",
    "pictogram": "\U0001f327",
    "eenheid": "mm/uur",
    "stap": 5,
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


def legenda() -> list[list]:
    """Kleurstops van deze laag als [waarde, "#rrggbb"]."""
    return kleur.hex_stops(KLEURSTOPS)


def _kalibratie(groep: h5py.Group) -> tuple[float, float, int]:
    """Leest de kalibratieformule GEO = a * PV + b en de nodata-waarde."""
    a, b, nodata = 0.01, 0.0, 65535
    cal = groep.get("calibration")
    if cal is not None:
        formule = cal.attrs.get("calibration_formulas")
        if formule is not None:
            tekst = formule[0] if isinstance(formule, np.ndarray) else formule
            tekst = tekst.decode() if isinstance(tekst, bytes) else str(tekst)
            m = re.search(r"GEO\s*=\s*([0-9.eE+-]+)\s*\*\s*PV\s*\+\s*([0-9.eE+-]+)", tekst)
            if m:
                a, b = float(m.group(1)), float(m.group(2))
        if "calibration_out_of_image" in cal.attrs:
            nodata = int(np.ravel(cal.attrs["calibration_out_of_image"])[0])
    return a, b, nodata


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
            a, b, nodata = _kalibratie(groep)
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
