"""Client voor de KNMI Open Data API.

Documentatie: https://developer.dataplatform.knmi.nl/open-data-api
"""
import logging
import os

import requests

BASIS_URL = "https://api.dataplatform.knmi.nl/open-data/v1"
log = logging.getLogger(__name__)


class KNMIClient:
    """Kleine client voor lijst- en downloadverzoeken."""

    def __init__(self, api_key: str | None = None):
        sleutel = api_key or os.environ.get("KNMI_API_KEY")
        if not sleutel:
            raise RuntimeError(
                "Geen API-sleutel gevonden. Zet de omgevingsvariabele KNMI_API_KEY."
            )
        self.sessie = requests.Session()
        self.sessie.headers["Authorization"] = sleutel

    def lijst_recent(self, dataset: str, versie: str, aantal: int = 3) -> list[dict]:
        """Geeft de meest recente bestanden terug, nieuwste eerst."""
        url = f"{BASIS_URL}/datasets/{dataset}/versions/{versie}/files"
        antwoord = self.sessie.get(
            url,
            params={"maxKeys": aantal, "orderBy": "created", "sorting": "desc"},
            timeout=30,
        )
        antwoord.raise_for_status()
        return antwoord.json().get("files", [])

    def download(self, dataset: str, versie: str, bestandsnaam: str, doelpad: str) -> str:
        """Downloadt een bestand via een tijdelijke URL. Geeft het doelpad terug."""
        url = (
            f"{BASIS_URL}/datasets/{dataset}/versions/{versie}"
            f"/files/{bestandsnaam}/url"
        )
        antwoord = self.sessie.get(url, timeout=30)
        antwoord.raise_for_status()
        tijdelijke_url = antwoord.json()["temporaryDownloadUrl"]
        # De tijdelijke URL vereist geen Authorization-header
        data = requests.get(tijdelijke_url, timeout=120)
        data.raise_for_status()
        tmp = doelpad + ".part"
        with open(tmp, "wb") as f:
            f.write(data.content)
        os.replace(tmp, doelpad)
        log.info("Gedownload: %s (%d kB)", bestandsnaam, len(data.content) // 1024)
        return doelpad
