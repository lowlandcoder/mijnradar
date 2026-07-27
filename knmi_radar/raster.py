"""Doelraster en herprojectie voor de HDF5-bestanden van het KNMI.

Het KNMI-raster staat in een polaire stereografische projectie (eenheid km).
Voor een correcte weergave in Leaflet wordt het omgerekend naar webmercator
(EPSG:3857). Daarvoor wordt eenmalig een opzoektabel gemaakt, met voor elke
doelpixel de bijbehorende bronpixel, die op schijf wordt bewaard.

Deze module is alleen nodig voor lagen die ruwe HDF5-bestanden verwerken, zoals
de neerslagradar. Lagen die hun gegevens via de kaartdienst van het KNMI (WCS)
ophalen krijgen het raster al in webmercator aangeleverd en slaan dit over.
"""
import hashlib
import logging
import os
import re

import h5py
import numpy as np
from pyproj import Transformer

log = logging.getLogger(__name__)

# Standaard projectie van het KNMI-radarcomposiet (fallback als het bestand
# geen projectieparameters bevat). Eenheid: kilometer.
KNMI_PROJ4 = (
    "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=60 "
    "+a=6378.14 +b=6356.75 +x_0=0 +y_0=0"
)

# Doelgebied in graden (webmercator-uitsnede rond Nederland en omgeving).
# Alle lagen gebruiken dit raster, zodat ze exact op elkaar passen.
# LET OP: de volgorde van de sleutels telt mee in de cachesleutel van de
# opzoektabel. Wijzigen betekent dat de tabel opnieuw wordt berekend.
DOEL = {
    "west": 0.0,
    "oost": 11.0,
    "zuid": 48.8,
    "noord": 55.97,
    "breedte": 704,
    "hoogte": 832,
}


def grenzen() -> list[list[float]]:
    """Kaartgrenzen voor Leaflet: [[zuid, west], [noord, oost]]."""
    return [[DOEL["zuid"], DOEL["west"]], [DOEL["noord"], DOEL["oost"]]]


def lees_projectie(h5: h5py.File) -> tuple[str, float, float]:
    """Leest proj4-parameters en rasteroffsets uit het bestand.

    Geeft (proj4, kolom_offset, rij_offset) terug. Valt terug op de
    bekende KNMI-standaardwaarden als attributen ontbreken.
    """
    proj4 = KNMI_PROJ4
    kol_offset, rij_offset = 0.0, 3650.0  # standaard NL-composiet
    geo = h5.get("geographic")
    if geo is not None:
        kol_offset = float(np.ravel(geo.attrs.get("geo_column_offset", [kol_offset]))[0])
        rij_offset = float(np.ravel(geo.attrs.get("geo_row_offset", [rij_offset]))[0])
        mp = geo.get("map_projection")
        if mp is not None and "projection_proj4_params" in mp.attrs:
            ruw = mp.attrs["projection_proj4_params"]
            proj4 = ruw.decode() if isinstance(ruw, bytes) else str(ruw)
    return proj4, kol_offset, rij_offset


def opzoektabel(proj4: str, kol_offset: float, rij_offset: float,
                vorm: tuple[int, int], cache_map: str) -> np.ndarray:
    """Maakt (of laadt) de opzoektabel doelpixel -> bronindex.

    De tabel bevat per doelpixel de platte index in het bronraster,
    of -1 als de doelpixel buiten het bronraster valt.

    De sleutel voor de bestandsnaam is een vaste hash (hashlib.sha256) van
    de projectieparameters, niet Pythons ingebouwde hash(): die laatste is
    voor tekst per processtart willekeurig gezouten, waardoor de tabel bij
    elke run (elke 5 minuten, via systemd) opnieuw berekend en nooit
    hergebruikt werd. Dat liet de schijf langzaam vollopen (~900 MB/dag,
    opgelost op 2026-07-15).
    """
    ruwe_sleutel = repr((proj4, kol_offset, rij_offset, vorm, tuple(DOEL.values()))).encode()
    sleutel = hashlib.sha256(ruwe_sleutel).hexdigest()[:16]
    pad = os.path.join(cache_map, f"lookup_{sleutel}.npy")
    if os.path.exists(pad):
        return np.load(pad)
    log.info("Opzoektabel wordt eenmalig berekend...")
    rijen, kolommen = vorm
    b, h = DOEL["breedte"], DOEL["hoogte"]
    # Doelpixelcentra in webmercator
    naar_mercator = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x_west, y_zuid = naar_mercator.transform(DOEL["west"], DOEL["zuid"])
    x_oost, y_noord = naar_mercator.transform(DOEL["oost"], DOEL["noord"])
    xs = np.linspace(x_west, x_oost, b)
    ys = np.linspace(y_noord, y_zuid, h)  # bovenste rij eerst
    gx, gy = np.meshgrid(xs, ys)
    # Webmercator -> lon/lat -> KNMI-projectie (km)
    # Het KNMI definieert de ellipsoide in kilometers; pyproj accepteert dat
    # niet. Daarom wordt de ellipsoide naar meters geschaald en het resultaat
    # daarna weer naar kilometers teruggerekend.
    schaal = 1.0
    m = re.search(r"\+a=([0-9.]+)", proj4)
    if m and float(m.group(1)) < 10000:
        schaal = 1000.0
        proj4 = re.sub(
            r"\+(a|b)=([0-9.]+)",
            lambda t: f"+{t.group(1)}={float(t.group(2)) * 1000:.1f}",
            proj4,
        )
    naar_lonlat = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    lon, lat = naar_lonlat.transform(gx, gy)
    naar_knmi = Transformer.from_crs("EPSG:4326", proj4, always_xy=True)
    px, py = naar_knmi.transform(lon, lat)
    px, py = px / schaal, py / schaal
    # Projectiecoordinaten (km) -> kolom en rij in het bronraster
    kol = np.round(px - kol_offset).astype(np.int64)
    rij = np.round(-py - rij_offset).astype(np.int64)
    geldig = (kol >= 0) & (kol < kolommen) & (rij >= 0) & (rij < rijen)
    tabel = np.where(geldig, rij * kolommen + kol, -1)
    os.makedirs(cache_map, exist_ok=True)
    np.save(pad, tabel)
    log.info("Opzoektabel bewaard: %s", pad)
    return tabel


def toepassen(tabel: np.ndarray, data: np.ndarray, buiten) -> np.ndarray:
    """Herprojecteert een bronraster met de opzoektabel naar het doelraster."""
    plat = data.ravel()
    doel = np.where(tabel >= 0, plat[np.clip(tabel, 0, plat.size - 1)], buiten)
    return doel.reshape(DOEL["hoogte"], DOEL["breedte"])
