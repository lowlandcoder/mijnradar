# MijnRadar

Weerkaart voor Nederland met meerdere lagen: historie van de laatste 2 uur en,
waar de bron dat toelaat, een korte-termijnverwachting. Leaflet-kaart in de
gedeelde huisstijl (nog niet volledig doorgevoerd, zie Aandachtspunten in
OVERZICHT.md).

## Wat de pagina toont

- Een schuivende reeks PNG-frames over de kaart, met een tijdbalk en een
  afspeelknop. Alle lagen delen hetzelfde doelraster van 704 bij 832
  beeldpunten, zodat ze exact op elkaar passen.
- De browser leest de kaartgrenzen, de beschikbare lagen en hun frames uit
  `frames.json`. Ook de legenda komt daaruit, zodat de kleuren altijd gelijk
  zijn aan wat de server rendert.
- De laag is te kiezen in de bovenbalk, of met `?laag=<sleutel>` in de URL.
  Bij een enkele laag blijft die keuze verborgen.

## Lagen

| Laag | Bron | Interval | Verwachting |
| --- | --- | --- | --- |
| `neerslag` | KNMI Open Data, radarcomposiet en nowcast | 5 minuten | ja, 2 uur |
| `zon` | KNMI MSG-CPP, satelliet Meteosat | 15 minuten | nee |

De zonlaag toont niet de kale zonnestraling maar de **helderheidsindex**: de
gemeten straling gedeeld door de straling bij een onbewolkte hemel. Een kaart in
W/m² laat vooral de zonnestand zien en is 's ochtends overal donker, ook bij een
strakblauwe hemel. De index is daar ongevoelig voor. 's Nachts is de index
betekenisloos; die beeldpunten blijven doorzichtig.

Een laag draait alleen mee als de bijbehorende API-sleutel is ingesteld. Zonder
`KNMI_WMS_API_KEY` blijft de zonlaag eenvoudigweg weg, zonder foutmelding.

## Techniek

- **Webpagina:** `index.html`, rechtstreeks vanuit de browser, geen buildstap.
- **Backend:** Python-module `knmi_radar/`, gedraaid als systemd-dienst
  (`mijnradar.service`) met een timer die elke 5 minuten ververst
  (`mijnradar.timer`).

Indeling van de module:

    bronnen/opendata.py   KNMI Open Data API: losse bestanden ophalen
    bronnen/ogc.py        KNMI-kaartdienst (ADAGUC): rasters via WCS
    raster.py             doelraster, projectie en opzoektabel (alleen HDF5)
    kleur.py              kleurstops naar RGBA, logaritmisch of lineair
    lagen/neerslag.py     kleurschaal, kalibratie en reeksen van de neerslaglaag
    lagen/zon.py          helderheidsindex en weergave van de zonlaag
    alert.py              weeralert bij verwachte neerslag
    run.py                loopt over de beschikbare lagen, schrijft frames.json

Een laag beschrijft zichzelf volledig: naam, eenheid, tijdstap, legenda en een
functie die de PNG's bijwerkt. Een nieuwe laag toevoegen is daardoor een nieuw
bestand in `lagen/` plus een regel in `lagen/__init__.py`, zonder wijziging in
`run.py` of in de kaartcode.

**Projectie.** De neerslaglaag komt binnen als HDF5 in een polair stereografisch
raster (km) en wordt omgerekend naar webmercator via een eenmalig berekende
opzoektabel, die op schijf wordt bewaard. De zonlaag heeft dat niet nodig: de
kaartdienst van het KNMI levert het raster al uitgesneden en herprojecteerd op
precies het doelraster.

## Instellingen

Kopieer `mijnradar.env.example` naar `/etc/mijnradar/mijnradar.env` op de
server en vul de sleutels in. Dit bestand bevat geheimen (ook het
SMTP-wachtwoord van het weeralert) en hoort nooit op GitHub.

Er zijn twee losse sleutels nodig, allebei aan te vragen in de API Catalog van
het KNMI Developer Portal. Ze zijn niet uitwisselbaar: de Open Data-sleutel
geeft op de kaartdienst een 403.

- `KNMI_API_KEY` — Open Data API, voor de neerslaglaag.
- `KNMI_WMS_API_KEY` — Web Map Service, voor de zonlaag. Deze sleutel dekt ook
  de WCS-verzoeken, want die lopen via hetzelfde adres.

## Weeralert

De module `knmi_radar/alert.py` controleert bij elk nieuw nowcastbestand of
er binnen de ingestelde straal rond het punt (standaard 10 km rond Haarlem)
neerslag wordt verwacht van ten minste de drempel (standaard 1 mm/uur). Zo
ja, dan gaat er een e-mail naar de adressen in `ALERT_NAAR`, met het
verwachte begintijdstip en de zwaarste intensiteit. Na een alert blijft het
stil tot de wachttijd om is (standaard 6 uur); dat tijdstip staat in
`status.json` in de cachemap. Alle waarden zijn instelbaar in
`/etc/mijnradar/mijnradar.env` (zie `mijnradar.env.example`). Het alert
staat uit zolang `ALERT_NAAR` of `SMTP_HOST` leeg is; een fout in het alert
breekt het renderen niet.

## Serveronderdelen

- Code en virtualenv: `/opt/mijnradar/` (module `knmi_radar/`,
  `requirements.txt`).
- Instellingen: `/etc/mijnradar/mijnradar.env`.
- Werkmap (tijdelijke HDF5-bestanden): `/var/lib/mijnradar/werk/`.
- Cache (opzoektabel voor de projectie-omzetting): `/var/lib/mijnradar/cache/`.
- Uitvoer voor de webpagina: `/var/www/mijnradar/data/` (`frames.json`,
  `history/`, `forecast/`, `zon/`).
- Systemd-eenheden: `mijnradar.service` en `mijnradar.timer`, geïnstalleerd
  in `/etc/systemd/system/` (bronbestanden in dit repository onder
  `systemd/`).

Publiceren gaat met het generieke script van lab023:

    ~/publiceer.sh mijnradar

Omdat mijnradar een achterkant heeft, staan er twee extra bestanden in de repo.
`.publiceer-negeer` houdt `knmi_radar/` en `systemd/` uit de docroot, en
`publiceer-extra.sh` zet die op hun eigen plek en herstart de dienst.

## Bekende storing en oplossing (2026-07-15)

De cachesleutel voor de opzoektabel werd berekend met Pythons ingebouwde
`hash()` op een tupel dat ook tekst bevatte. Tekst-hashing is sinds Python
3.3 per processtart willekeurig gezouten (beveiligingsmaatregel); omdat de
dienst elke 5 minuten als nieuw proces start, kreeg de tabel telkens een
andere bestandsnaam en werd de bestaande cache nooit herkend. Gevolg: bij
elke run een nieuw bestand van circa 4,4 MB, oplopend tot 7,3 GB in 8 dagen
(circa 900 MB/dag), met een bijna volgelopen schijf als gevolg.

Opgelost in `knmi_radar/render.py` door de sleutel te berekenen met
`hashlib.sha256` in plaats van `hash()`. Die is wel stabiel tussen
processen. Na de wijziging is bevestigd dat een herhaalde run dezelfde
opzoektabel hergebruikt (geen nieuw bestand, geen herberekening). De 1668
verouderde cachebestanden zijn daarna verwijderd.
