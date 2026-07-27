"""Client voor de kaartdienst van het KNMI (ADAGUC, OGC-standaarden).

De dienst levert de MSG-CPP-satellietproducten. Het grote voordeel boven de
losse bestanden uit de Open Data API: met een WCS-verzoek wordt het raster al
serverzijdig uitgesneden en herprojecteerd naar de gevraagde projectie. Voor
mijnradar betekent dat webmercator op precies het doelraster, waardoor de
opzoektabel uit `raster` voor deze lagen niet nodig is.

Documentatie: https://developer.dataplatform.knmi.nl/wms
Producten:    https://msgcpp.knmi.nl/data-access.html

Het antwoord is NetCDF4, wat intern HDF5 is. h5py leest dat rechtstreeks, dus
er is geen extra afhankelijkheid nodig.
"""
import datetime as dt
import io
import logging
import os

import h5py
import numpy as np
import requests

BASIS_URL = "https://api.dataplatform.knmi.nl/wms/adaguc-server"
log = logging.getLogger(__name__)

# Namen die in elk antwoord voorkomen en dus niet het gevraagde veld zijn
HULPVELDEN = {"crs", "time", "x", "y", "lat", "lon"}


class OGCClient:
    """Kleine WCS-client: vraagt een raster op voor een tijdstip."""

    def __init__(self, dataset: str, api_key: str | None = None):
        sleutel = api_key or os.environ.get("KNMI_WMS_API_KEY")
        if not sleutel:
            raise RuntimeError(
                "Geen sleutel voor de kaartdienst gevonden. "
                "Zet de omgevingsvariabele KNMI_WMS_API_KEY."
            )
        self.dataset = dataset
        self.sessie = requests.Session()
        self.sessie.headers["Authorization"] = sleutel

    def _verzoek(self, coverage: str, tijd: str, bbox: str,
                 breedte: int, hoogte: int) -> bytes:
        params = {
            "dataset": self.dataset,
            "service": "wcs",
            "request": "getcoverage",
            "coverage": coverage,
            "CRS": "EPSG:3857",
            "BBOX": bbox,
            "WIDTH": breedte,
            "HEIGHT": hoogte,
            "FORMAT": "NetCDF4",
            "time": tijd,
        }
        antwoord = self.sessie.get(BASIS_URL, params=params, timeout=60)
        antwoord.raise_for_status()
        if not antwoord.content.startswith(b"\x89HDF"):
            raise RuntimeError("Geen NetCDF terug voor %s: %s"
                               % (coverage, antwoord.text[:200]))
        return antwoord.content

    def veld(self, coverage: str, tijd: str, bbox: str,
             breedte: int, hoogte: int) -> tuple[np.ndarray, dt.datetime]:
        """Haalt een raster op. Geeft (waarden, tijdstip) terug.

        Ontbrekende waarden worden NaN, zodat een laag zelf kan bepalen wat
        daarmee gebeurt.
        """
        inhoud = self._verzoek(coverage, tijd, bbox, breedte, hoogte)
        with h5py.File(io.BytesIO(inhoud), "r") as f:
            naam = next(k for k in f.keys() if k not in HULPVELDEN)
            veld = f[naam]
            data = np.array(veld).astype("float64").squeeze()
            vul = veld.attrs.get("_FillValue")
            if vul is not None:
                data = np.where(data == float(np.ravel(vul)[0]), np.nan, data)
            seconden = float(np.ravel(f["time"])[0])
        return data, dt.datetime.fromtimestamp(seconden, dt.timezone.utc)

    def nieuwste_tijd(self, coverage: str, bbox: str) -> dt.datetime:
        """Geeft het tijdstip van het nieuwste beschikbare beeld.

        Vraagt bewust een raster van 2 bij 2 beeldpunten op: het gaat alleen om
        het tijdstip, niet om de inhoud. Dat scheelt bij elke run een volledig
        beeld aan dataverkeer, terwijl de dienst maar elk kwartier ververst.
        """
        _, tijdstip = self.veld(coverage, "current", bbox, 2, 2)
        return tijdstip
