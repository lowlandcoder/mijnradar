"""Weeralert: mailt wanneer de nowcast neerslag verwacht rond een punt.

Wordt aangeroepen vanuit run.py zodra er een nieuw nowcastbestand is.
De controle kijkt per verwachtingsframe (5-minutenstappen, 2 uur vooruit)
naar de hoogste intensiteit binnen een straal rond het ingestelde punt
(standaard Haarlem). Ligt die op of boven de drempel, dan gaat er een
e-mail uit. Na een verzonden alert blijft het stil tot de wachttijd om is;
dat tijdstip staat in status.json (sleutel "laatste_alert").

Instellingen via /etc/mijnradar/mijnradar.env (zie mijnradar.env.example).
Zonder ALERT_NAAR of SMTP_HOST staat het alert uit.
"""
import datetime as dt
import logging
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

import h5py
import numpy as np
from pyproj import Transformer

from knmi_radar.raster import kalibratie, lees_projectie

log = logging.getLogger("mijnradar.alert")


def _instellingen() -> dict:
    naar = [a.strip() for a in os.environ.get("ALERT_NAAR", "").split(",")
            if a.strip()]
    return {
        "naar": naar,
        "straal_km": float(os.environ.get("ALERT_STRAAL_KM", "10")),
        "drempel": float(os.environ.get("ALERT_DREMPEL_MM_UUR", "1")),
        "wacht_uur": float(os.environ.get("ALERT_WACHT_UUR", "6")),
        "lat": float(os.environ.get("ALERT_LAT", "52.387")),
        "lon": float(os.environ.get("ALERT_LON", "4.646")),
        "plaats": os.environ.get("ALERT_PLAATS", "Haarlem"),
        "smtp_host": os.environ.get("SMTP_HOST", ""),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "smtp_user": os.environ.get("SMTP_USER", ""),
        "smtp_wachtwoord": os.environ.get("SMTP_PASSWORD", ""),
        "mail_van": os.environ.get("MAIL_VAN", os.environ.get("SMTP_USER", "")),
        "mail_naam": os.environ.get("MAIL_NAAM", "MijnRadar"),
    }


def _rooster_positie(h5: h5py.File, lat: float, lon: float) -> tuple[int, int]:
    """Zet lengte- en breedtegraad om naar (rij, kolom) in het bronraster.

    Zelfde omrekening als in raster.opzoektabel: de KNMI-ellipsoide staat
    in kilometers en wordt voor pyproj naar meters geschaald.
    """
    proj4, kol_offset, rij_offset = lees_projectie(h5)
    schaal = 1.0
    m = re.search(r"\+a=([0-9.]+)", proj4)
    if m and float(m.group(1)) < 10000:
        schaal = 1000.0
        proj4 = re.sub(
            r"\+(a|b)=([0-9.]+)",
            lambda t: f"+{t.group(1)}={float(t.group(2)) * 1000:.1f}",
            proj4,
        )
    naar_knmi = Transformer.from_crs("EPSG:4326", proj4, always_xy=True)
    px, py = naar_knmi.transform(lon, lat)
    px, py = px / schaal, py / schaal
    return int(round(-py - rij_offset)), int(round(px - kol_offset))


def _max_rond(groep: h5py.Group, rij: int, kol: int, straal: int) -> float:
    """Hoogste intensiteit (mm/uur) binnen de straal (in rasterpixels, 1 km)."""
    data = np.asarray(groep["image_data"])
    r0, r1 = max(rij - straal, 0), min(rij + straal + 1, data.shape[0])
    k0, k1 = max(kol - straal, 0), min(kol + straal + 1, data.shape[1])
    if r0 >= r1 or k0 >= k1:
        return 0.0
    blok = data[r0:r1, k0:k1].astype(np.float64)
    rr, kk = np.ogrid[r0:r1, k0:k1]
    masker = (rr - rij) ** 2 + (kk - kol) ** 2 <= straal ** 2
    if not masker.any():
        return 0.0
    a, b, nodata = kalibratie(groep)
    mm_per_uur = np.where(blok == nodata, 0.0, (blok * a + b) * 12.0)
    return float(mm_per_uur[masker].max())


def _verstuur(inst: dict, treffers: list) -> None:
    tz = ZoneInfo("Europe/Amsterdam")
    eerste_t = treffers[0][0]
    piek = max(p for _, p in treffers)
    tijd = (eerste_t.astimezone(tz).strftime("%H:%M")
            if eerste_t else "binnen 2 uur")

    onderwerp = f"Neerslag verwacht rond {inst['plaats']} omstreeks {tijd}"
    regels = [
        f"Er wordt neerslag verwacht binnen {inst['straal_km']:g} km "
        f"van {inst['plaats']}.",
        "",
        f"Eerste neerslag: omstreeks {tijd}",
        f"Zwaarste intensiteit komende 2 uur: {piek:.1f} mm/uur",
        "",
        "Radar: https://mijnradar.lab023.nl",
    ]
    msg = MIMEText("\n".join(regels), "plain", "utf-8")
    msg["Subject"] = onderwerp
    msg["From"] = formataddr((inst["mail_naam"], inst["mail_van"]))
    msg["To"] = ", ".join(inst["naar"])

    if inst["smtp_port"] == 465:
        server = smtplib.SMTP_SSL(inst["smtp_host"], inst["smtp_port"],
                                  timeout=30)
    else:
        server = smtplib.SMTP(inst["smtp_host"], inst["smtp_port"], timeout=30)
        server.starttls()
    try:
        if inst["smtp_user"]:
            server.login(inst["smtp_user"], inst["smtp_wachtwoord"])
        server.sendmail(inst["mail_van"], inst["naar"], msg.as_string())
    finally:
        server.quit()
    log.info("Weeralert verstuurd naar %d adres(sen): eerste neerslag %s, "
             "piek %.1f mm/uur", len(inst["naar"]), tijd, piek)


def controleer(h5_pad: str, starttijd: dt.datetime | None, status: dict) -> None:
    """Controleert het nowcastbestand en mailt bij verwachte neerslag.

    Zet na verzending status["laatste_alert"]; binnen de wachttijd wordt
    er niet opnieuw gecontroleerd of gemaild.
    """
    inst = _instellingen()
    if not inst["naar"] or not inst["smtp_host"]:
        log.debug("Weeralert niet ingesteld (ALERT_NAAR of SMTP_HOST leeg)")
        return

    laatste = status.get("laatste_alert")
    if laatste:
        verstreken = (dt.datetime.now(dt.timezone.utc)
                      - dt.datetime.fromisoformat(laatste))
        if verstreken < dt.timedelta(hours=inst["wacht_uur"]):
            return

    straal = max(1, int(round(inst["straal_km"])))
    treffers = []
    with h5py.File(h5_pad, "r") as h5:
        rij, kol = _rooster_positie(h5, inst["lat"], inst["lon"])
        groepen = sorted(
            (g for g in h5.keys() if re.fullmatch(r"image\d+", g)),
            key=lambda g: int(g[5:]),
        )
        for i, naam in enumerate(groepen):
            piek = _max_rond(h5[naam], rij, kol, straal)
            if piek >= inst["drempel"]:
                t = (starttijd + dt.timedelta(minutes=5 * i)
                     if starttijd else None)
                treffers.append((t, piek))

    if not treffers:
        return
    _verstuur(inst, treffers)
    status["laatste_alert"] = dt.datetime.now(dt.timezone.utc).isoformat()
