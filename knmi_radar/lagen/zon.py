"""Zonlaag: waar komt de zon door de bewolking?

Bron: MSG-CPP van het KNMI, afgeleid uit de SEVIRI-satelliet op Meteosat. Twee
velden per tijdstip:

  sds      de gemeten zonnestraling aan het oppervlak, W/m2
  sds_cs   dezelfde straling bij een onbewolkte hemel, W/m2

De kaart toont niet de kale straling maar de helderheidsindex, sds gedeeld door
sds_cs. Een kaart in W/m2 laat vooral de zonnestand zien: die is 's ochtends
overal laag, ook bij een strakblauwe hemel. De index is daar ongevoelig voor en
beantwoordt de vraag waar de zon doorkomt.

De rasters komen al herprojecteerd binnen op het doelraster, dus de opzoektabel
uit `raster` is hier niet nodig.

Interval: 15 minuten, met ongeveer een kwartier vertraging. De dienst levert
alleen waarnemingen, geen verwachting; de reeks `forecast` blijft daarom leeg.
"""
import datetime as dt
import logging
import os

import numpy as np
from PIL import Image

from knmi_radar.bronnen.ogc import OGCClient
from knmi_radar.hulp import iso
from knmi_radar.raster import DOEL, bbox_mercator

log = logging.getLogger(__name__)

SLEUTEL = "zon"

DATASET = "msg_cpp_products"
VELD_GEMETEN = "surface_downwelling_shortwave_flux_in_air"
VELD_HELDER = "surface_downwelling_shortwave_flux_in_air_assuming_clear_sky"

VENSTER_MINUTEN = 120   # 2 uur historie, gelijk aan de neerslaglaag
STAP_MINUTEN = 15       # de satelliet ververst elk kwartier

# Onder deze heldere-hemelwaarde (W/m2) is de index betekenisloos: het is dan
# nacht of schemer. Die beeldpunten blijven volledig doorzichtig.
NACHT_DREMPEL = 20.0

# Weergave, vastgesteld na twee proefrondes op de kaart. Geel waar de zon
# doorkomt, en onder WAAS_GRENS een zwakke grijstint zodat zware bewolking te
# onderscheiden blijft van nacht en van ontbrekende gegevens.
GEEL = np.array([253, 224, 71])
GRIJS = np.array([148, 163, 184])
GLOED_VANAF = 0.50
GLOED_EXPONENT = 1.2
GLOED_MAXALFA = 190
WAAS_MAXALFA = 70
WAAS_GRENS = 0.25

BESCHRIJVING = {
    "naam": "Zon",
    "pictogram": "☀️",
    "eenheid": "index",
    "stap": STAP_MINUTEN,
    "schaal": "lineair",
    "bron": "KNMI MSG-CPP, satelliet Meteosat",
}


def beschikbaar() -> bool:
    """De laag draait alleen mee als er een sleutel voor de kaartdienst is."""
    return bool(os.environ.get("KNMI_WMS_API_KEY"))


def legenda() -> list[list]:
    """Legenda van bewolkt naar zonnig.

    De schaal in de pagina kent geen doorzichtigheid, dus het midden is bijna
    wit: dat is precies waar de laag de basiskaart ongemoeid laat.
    """
    return [[0.0, "#94a3b8"], [0.25, "#cbd5e1"], [0.5, "#f8fafc"],
            [0.75, "#fef08a"], [1.0, "#fde047"]]


def index_en_nacht(gemeten: np.ndarray, helder: np.ndarray):
    """Rekent de twee stralingsvelden om naar een index van 0 tot 1."""
    nacht = ~np.isfinite(helder) | (helder < NACHT_DREMPEL)
    index = np.clip(np.where(nacht, 0.0, gemeten / np.maximum(helder, 1e-6)),
                    0.0, 1.0)
    return np.nan_to_num(index), nacht


def kleuren(index: np.ndarray, nacht: np.ndarray) -> np.ndarray:
    """Zet de index om naar een RGBA-beeld (zonneglans met waas)."""
    beeld = np.zeros(index.shape + (4,), dtype=np.uint8)
    warmte = np.clip((index - GLOED_VANAF) / (1.0 - GLOED_VANAF),
                     0.0, 1.0) ** GLOED_EXPONENT
    alfa_geel = warmte * GLOED_MAXALFA
    alfa_waas = np.clip((WAAS_GRENS - index) / WAAS_GRENS, 0.0, 1.0) * WAAS_MAXALFA
    kies_waas = alfa_waas > alfa_geel
    beeld[..., :3] = np.where(kies_waas[..., None], GRIJS, GEEL)
    beeld[..., 3] = np.where(nacht, 0, np.maximum(alfa_geel, alfa_waas)).astype(np.uint8)
    return beeld


def verwerk(data_map: str, werk_map: str, cache_map: str, status: dict) -> dict:
    """Zorgt dat voor elk kwartier in het venster een PNG bestaat."""
    client = OGCClient(DATASET)
    bbox = bbox_mercator()
    uitvoer = os.path.join(data_map, "zon")
    os.makedirs(uitvoer, exist_ok=True)

    nieuwste = client.nieuwste_tijd(VELD_GEMETEN, bbox)
    aantal = VENSTER_MINUTEN // STAP_MINUTEN + 1
    tijden = [nieuwste - dt.timedelta(minutes=STAP_MINUTEN * k)
              for k in range(aantal)][::-1]

    frames, fouten, nieuw = [], [], 0
    for tijdstip in tijden:
        png_naam = "zon_%s.png" % tijdstip.strftime("%Y%m%d%H%M")
        png_pad = os.path.join(uitvoer, png_naam)
        if not os.path.exists(png_pad):
            stempel = iso(tijdstip)
            try:
                gemeten, _ = client.veld(VELD_GEMETEN, stempel, bbox,
                                         DOEL["breedte"], DOEL["hoogte"])
                helder, _ = client.veld(VELD_HELDER, stempel, bbox,
                                        DOEL["breedte"], DOEL["hoogte"])
            except Exception as fout:  # noqa: BLE001
                log.warning("Zon %s overgeslagen: %s", stempel, fout)
                fouten.append("zon %s: %s" % (stempel, fout))
                continue
            index, nacht = index_en_nacht(gemeten, helder)
            tijdelijk = png_pad + ".part"
            Image.fromarray(kleuren(index, nacht), "RGBA").save(tijdelijk,
                                                               optimize=True)
            os.replace(tijdelijk, png_pad)
            nieuw += 1
        frames.append({"time": iso(tijdstip), "file": "zon/%s" % png_naam})

    # Opruimen: PNG's buiten het venster verwijderen
    geldig = {f["file"].split("/")[-1] for f in frames}
    for naam in os.listdir(uitvoer):
        if naam.startswith("zon_") and naam not in geldig:
            os.remove(os.path.join(uitvoer, naam))

    if nieuw:
        log.info("Zon: %d nieuwe frames (nieuwste %s)", nieuw, iso(nieuwste))
    return {"history": frames, "forecast": [], "fouten": fouten}
