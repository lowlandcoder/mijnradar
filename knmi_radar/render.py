"""Zet KNMI HDF5-radarbestanden om naar transparante PNG's voor Leaflet.

Werkwijze:
1. Het KNMI-raster staat in een polaire stereografische projectie (eenheid km).
2. Voor een correcte weergave in Leaflet wordt het raster omgerekend naar
   webmercator (EPSG:3857). Daarvoor wordt eenmalig een opzoektabel gemaakt
   (voor elke doelpixel de bijbehorende bronpixel) en op schijf bewaard.
3. Waarden worden omgerekend van 0,01 mm per 5 minuten naar mm per uur en
   ingekleurd met een vaste kleurenschaal. Droog en 'geen data' zijn transparant.
"""
import hashlib
import logging
import os
import re

import h5py
import numpy as np
from PIL import Image
from pyproj import Transformer

log = logging.getLogger(__name__)

# Standaard projectie van het KNMI-radarcomposiet (fallback als het bestand
# geen projectieparameters bevat). Eenheid: kilometer.
KNMI_PROJ4 = (
    "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=60 "
    "+a=6378.14 +b=6356.75 +x_0=0 +y_0=0"
)

# Doelgebied in graden (webmercator-uitsnede rond Nederland en omgeving)
DOEL = {
    "west": 0.0,
    "oost": 11.0,
    "zuid": 48.8,
    "noord": 55.97,
    "breedte": 704,
    "hoogte": 832,
}

# Kleurenschaal: neerslagintensiteit in mm/uur naar RGBA
# (aansluitend op de stijl van de oude legenda)
KLEURSTOPS = [
    (0.1, (208, 240, 255, 90)),
    (0.3, (112, 200, 255, 150)),
    (1.0, (0, 102, 255, 190)),
    (3.0, (0, 204, 68, 210)),
    (10.0, (255, 238, 0, 225)),
    (30.0, (255, 102, 0, 240)),
    (100.0, (204, 0, 0, 255)),
]


def _lees_projectie(h5: h5py.File) -> tuple[str, float, float]:
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


def _opzoektabel(proj4: str, kol_offset: float, rij_offset: float,
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


def _kleur(mm_per_uur: np.ndarray) -> np.ndarray:
    """Zet intensiteit (mm/uur) om naar een RGBA-beeld."""
    h, w = mm_per_uur.shape
    beeld = np.zeros((h, w, 4), dtype=np.uint8)
    drempels = np.array([s[0] for s in KLEURSTOPS])
    kleuren = np.array([s[1] for s in KLEURSTOPS], dtype=np.float64)
    nat = mm_per_uur >= drempels[0]
    if nat.any():
        waarden = np.clip(mm_per_uur[nat], drempels[0], drempels[-1])
        # Logaritmische interpolatie past beter bij neerslagintensiteit
        positie = np.interp(np.log10(waarden), np.log10(drempels),
                            np.arange(len(drempels), dtype=np.float64))
        onder = np.floor(positie).astype(int)
        boven = np.minimum(onder + 1, len(drempels) - 1)
        frac = (positie - onder)[:, None]
        rgba = kleuren[onder] * (1 - frac) + kleuren[boven] * frac
        beeld[nat] = rgba.astype(np.uint8)
    return beeld


def render_bestand(h5_pad: str, uitvoer_map: str, cache_map: str,
                   prefix: str = "frame") -> list[dict]:
    """Rendert alle beeldgroepen in een HDF5-bestand naar PNG's.

    Geeft een lijst terug met per frame de PNG-bestandsnaam en, indien
    beschikbaar, het geldigheidstijdstip uit het bestand.
    """
    resultaat = []
    with h5py.File(h5_pad, "r") as h5:
        proj4, kol_offset, rij_offset = _lees_projectie(h5)
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
                tabel = _opzoektabel(proj4, kol_offset, rij_offset,
                                     data.shape, cache_map)
            a, b, nodata = _kalibratie(groep)
            plat = data.ravel()
            # Herprojecteren via de opzoektabel
            doel = np.where(tabel >= 0, plat[np.clip(tabel, 0, plat.size - 1)], nodata)
            doel = doel.reshape(DOEL["hoogte"], DOEL["breedte"])
            # 0,01 mm per 5 min -> mm/uur; nodata wordt 0 (transparant)
            mm_per_uur = np.where(doel == nodata, 0.0, (doel * a + b) * 12.0)
            beeld = _kleur(mm_per_uur)
            png_naam = f"{prefix}_{volgnummer:02d}.png"
            png_pad = os.path.join(uitvoer_map, png_naam)
            Image.fromarray(beeld, "RGBA").save(png_pad, optimize=True)
            tijdstip = None
            ruw = groep.attrs.get("image_datetime_valid")
            if ruw is not None:
                ruw = np.ravel(ruw)[0]
                tijdstip = ruw.decode() if isinstance(ruw, bytes) else str(ruw)
            resultaat.append({"png": png_naam, "tijd_attr": tijdstip})
    return resultaat


def grenzen() -> list[list[float]]:
    """Kaartgrenzen voor Leaflet: [[zuid, west], [noord, oost]]."""
    return [[DOEL["zuid"], DOEL["west"]], [DOEL["noord"], DOEL["oost"]]]
