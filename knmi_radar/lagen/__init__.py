"""Lagen van mijnradar.

Elke laag beschrijft zichzelf volledig: naam, eenheid, tijdstap, kleurschaal en
een functie die de brondata naar PNG's rendert. Een nieuwe laag toevoegen
betekent daardoor een nieuw bestand in deze map plus een regel hieronder,
zonder wijziging in de kaartcode of in run.py.

Elke laagmodule biedt:
    SLEUTEL         naam in frames.json en in de URL (?laag=...)
    BESCHRIJVING    naam, pictogram, eenheid, tijdstap, schaal en bron
    beschikbaar()   draait de laag mee? Meestal: is er een API-sleutel?
    legenda()       kleurstops als [waarde, "#rrggbb"]
    verwerk()       werkt de PNG's bij en geeft history, forecast en fouten
"""
from knmi_radar.lagen import neerslag, zon

# Volgorde bepaalt de volgorde van de knoppen in de bovenbalk
ALLE = {module.SLEUTEL: module for module in (neerslag, zon)}
