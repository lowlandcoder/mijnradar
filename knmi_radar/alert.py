"""Weeralert: meldt wanneer de nowcast neerslag verwacht rond een punt.

Wordt aangeroepen vanuit lagen/neerslag.py zodra er een nieuw nowcastbestand
is. De controle kijkt per verwachtingsframe (5-minutenstappen, 2 uur vooruit)
naar de hoogste intensiteit binnen een straal rond het ingestelde punt
(standaard Haarlem). Ligt die op of boven de drempel, dan gaat er een melding
uit over alle ingestelde kanalen:

  mail   naar de adressen in ALERT_NAAR, via SMTP
  mqtt   als JSON op MQTT_TOPIC, waar Home Assistant een telefoonmelding
         van maakt

Een kanaal staat aan zodra de bijbehorende instellingen gevuld zijn. Staat
geen enkel kanaal aan, dan gebeurt er niets. Een storing in het ene kanaal
houdt het andere niet tegen; pas als geen enkel kanaal is gelukt, blijft de
rustperiode ongezet en volgt bij de volgende run een nieuwe poging.

Na een verstuurde melding blijft het stil tot de wachttijd om is. Dat tijdstip
staat in status.json (sleutel "laatste_alert") en geldt voor beide kanalen
samen, dus mail en telefoonmelding gaan altijd gelijk op.

Instellingen via /etc/mijnradar/mijnradar.env (zie mijnradar.env.example).

Beproeven zonder op regen te wachten:

    python -m knmi_radar.alert --proef
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from email.utils import formataddr
from zoneinfo import ZoneInfo

import h5py
import numpy as np
from pyproj import Transformer

from knmi_radar.raster import kalibratie, lees_projectie

log = logging.getLogger("mijnradar.alert")

PAGINA = "https://mijnradar.lab023.nl"


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
        "mqtt_host": os.environ.get("MQTT_HOST", ""),
        "mqtt_port": int(os.environ.get("MQTT_PORT", "1883")),
        "mqtt_topic": os.environ.get("MQTT_TOPIC", "mijnradar/neerslag"),
        "mqtt_user": os.environ.get("MQTT_USER", ""),
        "mqtt_wachtwoord": os.environ.get("MQTT_PASSWORD", ""),
        "mqtt_client_id": os.environ.get("MQTT_CLIENT_ID", "mijnradar-alert"),
    }


def _getal(waarde: float) -> str:
    """Getal met een komma als decimaalteken; ronde getallen zonder komma."""
    if abs(waarde - round(waarde)) < 0.05:
        return str(int(round(waarde)))
    return f"{waarde:.1f}".replace(".", ",")


# ── Zoeken in de verwachting ─────────────────────────────────────────────────

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


def _treffers(h5_pad: str, starttijd: dt.datetime | None,
              inst: dict) -> list[tuple[dt.datetime | None, float]]:
    """Frames waarin de piek binnen de straal op of boven de drempel ligt."""
    straal = max(1, int(round(inst["straal_km"])))
    gevonden = []
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
                gevonden.append((t, piek))
    return gevonden


# ── Het bericht ──────────────────────────────────────────────────────────────

def _bericht(inst: dict, treffers: list) -> dict:
    """Bouwt één bericht dat alle kanalen delen.

    Naast de kale getallen staan er een kant-en-klare titel en tekst in, zodat
    de automatisering in Home Assistant niets hoeft uit te rekenen.
    """
    tz = ZoneInfo("Europe/Amsterdam")
    nu = dt.datetime.now(dt.timezone.utc)
    eerste_t = treffers[0][0]
    piek = max(p for _, p in treffers)
    tijd = eerste_t.astimezone(tz).strftime("%H:%M") if eerste_t else None
    minuten = None
    if eerste_t:
        minuten = max(0, int(round((eerste_t - nu).total_seconds() / 60)))
    wanneer = f"omstreeks {tijd}" if tijd else "binnen 2 uur"

    return {
        "verzonden": nu.isoformat(timespec="seconds"),
        "plaats": inst["plaats"],
        "straal_km": inst["straal_km"],
        "drempel_mm_uur": inst["drempel"],
        "eerste_neerslag": (eerste_t.isoformat(timespec="seconds")
                            if eerste_t else None),
        "eerste_neerslag_lokaal": tijd,
        "minuten_tot_neerslag": minuten,
        "piek_mm_uur": round(piek, 1),
        "titel": f"Neerslag verwacht rond {inst['plaats']} {wanneer}",
        "tekst": (f"Eerste neerslag {wanneer}, tot {_getal(piek)} mm/uur "
                  f"binnen {_getal(inst['straal_km'])} km."),
        "url": PAGINA,
    }


# ── De kanalen ───────────────────────────────────────────────────────────────

def _verstuur_mail(inst: dict, melding: dict) -> None:
    regels = [
        f"Er wordt neerslag verwacht binnen {_getal(inst['straal_km'])} km "
        f"van {inst['plaats']}.",
        "",
        f"Eerste neerslag: {melding['eerste_neerslag_lokaal'] or 'binnen 2 uur'}",
        f"Zwaarste intensiteit komende 2 uur: "
        f"{_getal(melding['piek_mm_uur'])} mm/uur",
        "",
        f"Radar: {melding['url']}",
    ]
    msg = MIMEText("\n".join(regels), "plain", "utf-8")
    msg["Subject"] = melding["titel"]
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
    log.info("Weeralert gemaild naar %d adres(sen)", len(inst["naar"]))


def _verstuur_mqtt(inst: dict, melding: dict) -> None:
    """Zet het bericht als JSON op de broker.

    `publish.single` verbindt, wacht op de bevestiging van de broker en sluit
    daarna af. Dat past bij deze dienst, die als oneshot draait en meteen weer
    weg is; een losse publish zonder wachten kan bij qos 1 verloren gaan.

    Bewust zonder retain: met een bewaard bericht zou Home Assistant bij elke
    herstart de laatste melding opnieuw binnenkrijgen en opnieuw de telefoon
    laten trillen.
    """
    from paho.mqtt import publish as mqtt_publish  # pas nodig bij verzenden

    auth = None
    if inst["mqtt_user"]:
        auth = {"username": inst["mqtt_user"],
                "password": inst["mqtt_wachtwoord"]}
    mqtt_publish.single(
        inst["mqtt_topic"],
        payload=json.dumps(melding, ensure_ascii=False),
        qos=1,
        retain=False,
        hostname=inst["mqtt_host"],
        port=inst["mqtt_port"],
        client_id=inst["mqtt_client_id"],
        keepalive=30,
        auth=auth,
    )
    log.info("Weeralert op MQTT gezet: %s", inst["mqtt_topic"])


def _kanalen(inst: dict) -> dict:
    """De kanalen die aanstaan, op naam.

    Een kanaal staat aan zodra de instellingen die het nodig heeft gevuld zijn.
    Zelfde patroon als bij de lagen: geen instelling, geen kanaal.
    """
    kanalen = {}
    if inst["naar"] and inst["smtp_host"]:
        kanalen["mail"] = _verstuur_mail
    if inst["mqtt_host"] and inst["mqtt_topic"]:
        kanalen["mqtt"] = _verstuur_mqtt
    return kanalen


def _meld(inst: dict, kanalen: dict, melding: dict) -> bool:
    """Verstuurt over elk kanaal. Geeft terug of er iets is gelukt."""
    gelukt = []
    for naam, verstuur in kanalen.items():
        try:
            verstuur(inst, melding)
            gelukt.append(naam)
        except Exception as fout:  # noqa: BLE001
            log.error("Weeralert via %s mislukt: %s", naam, fout)
    if gelukt:
        log.info("Weeralert verstuurd via %s: eerste neerslag %s, piek "
                 "%.1f mm/uur", ", ".join(gelukt),
                 melding["eerste_neerslag_lokaal"] or "onbekend",
                 melding["piek_mm_uur"])
    return bool(gelukt)


# ── Aanroep vanuit de laag ───────────────────────────────────────────────────

def controleer(h5_pad: str, starttijd: dt.datetime | None, status: dict) -> None:
    """Controleert het nowcastbestand en meldt bij verwachte neerslag.

    Zet na een geslaagde verzending status["laatste_alert"]; binnen de
    wachttijd wordt er niet opnieuw gecontroleerd of gemeld.
    """
    inst = _instellingen()
    kanalen = _kanalen(inst)
    if not kanalen:
        log.debug("Weeralert niet ingesteld: geen enkel kanaal aan")
        return

    laatste = status.get("laatste_alert")
    if laatste:
        verstreken = (dt.datetime.now(dt.timezone.utc)
                      - dt.datetime.fromisoformat(laatste))
        if verstreken < dt.timedelta(hours=inst["wacht_uur"]):
            return

    treffers = _treffers(h5_pad, starttijd, inst)
    if not treffers:
        return

    if _meld(inst, kanalen, _bericht(inst, treffers)):
        status["laatste_alert"] = dt.datetime.now(dt.timezone.utc).isoformat()


# ── Proefbericht ─────────────────────────────────────────────────────────────

def _proef() -> int:
    """Zet een proefbericht op MQTT, zonder radargegevens en zonder mail.

    Bedoeld om de keten broker, Home Assistant en telefoon te controleren, ook
    bij droog weer. Raakt status.json niet aan, dus de rustperiode van het
    echte alert verandert er niet door.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    inst = _instellingen()
    if "mqtt" not in _kanalen(inst):
        print("MQTT staat uit. Vul MQTT_HOST en MQTT_TOPIC in "
              "/etc/mijnradar/mijnradar.env.", file=sys.stderr)
        return 1

    over_kwartier = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
    melding = _bericht(inst, [(over_kwartier, 4.2)])
    melding["proef"] = True
    melding["titel"] = "Proefmelding MijnRadar"
    melding["tekst"] = ("Proefbericht van mijnradar. Staat deze melding op de "
                        "telefoon, dan werkt de hele keten.")
    _verstuur_mqtt(inst, melding)
    print(f"Proefbericht op '{inst['mqtt_topic']}' gezet.")
    return 0


if __name__ == "__main__":
    ontleder = argparse.ArgumentParser(description="Weeralert van mijnradar")
    ontleder.add_argument("--proef", action="store_true",
                          help="Zet een proefbericht op MQTT")
    if ontleder.parse_args().proef:
        sys.exit(_proef())
    ontleder.print_help()
    sys.exit(2)
