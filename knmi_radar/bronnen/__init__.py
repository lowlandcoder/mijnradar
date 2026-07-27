"""Bronnen waar mijnradar zijn gegevens vandaan haalt.

  opendata.py   KNMI Open Data API: losse bestanden, gebruikt voor de
                HDF5-radarbestanden van de neerslaglaag.
  ogc.py        KNMI-kaartdienst (ADAGUC): rasters via WCS, al herprojecteerd
                naar de gevraagde projectie. Gebruikt voor de satellietlagen.

Beide vragen een eigen API-sleutel; die van de Open Data API werkt niet op de
kaartdienst en andersom.
"""
