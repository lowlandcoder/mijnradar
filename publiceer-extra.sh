#!/bin/bash
# ============================================================
#  publiceer-extra.sh — naverwerking voor mijnradar
#
#  Wordt aangeroepen door ~/publiceer.sh, direct na het kopieren van de
#  webbestanden naar /var/www/mijnradar. Zet de onderdelen die niet in de
#  docroot horen op hun eigen plek:
#
#    knmi_radar/             -> /opt/mijnradar/knmi_radar/
#    requirements.txt        -> /opt/mijnradar/
#    systemd/*.service|timer -> /etc/systemd/system/
#
#  Daarna worden de systemd-eenheden herladen en draait er meteen een run,
#  zodat het resultaat direct te zien is.
#
#  Wat hier bewust NIET gebeurt: /etc/mijnradar/mijnradar.env aanraken.
#  Dat bestand bevat geheimen en staat niet in de repo.
# ============================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOEL="/opt/mijnradar"

echo "   code bijwerken in $DOEL"
sudo install -d -m 755 "$DOEL"
sudo rsync -a --delete "$REPO/knmi_radar/" "$DOEL/knmi_radar/"
sudo install -m 644 "$REPO/requirements.txt" "$DOEL/requirements.txt"

# Pakketten bijwerken als requirements.txt is gewijzigd. Zonder wijziging
# is dit een lege handeling.
if [ -x "$DOEL/venv/bin/pip" ]; then
  echo "   pakketten controleren in de virtualenv"
  sudo "$DOEL/venv/bin/pip" install -q -r "$DOEL/requirements.txt"
fi

echo "   systemd-eenheden bijwerken"
sudo install -m 644 "$REPO/systemd/mijnradar.service" /etc/systemd/system/
sudo install -m 644 "$REPO/systemd/mijnradar.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mijnradar.timer >/dev/null

echo "   eenmalige run starten"
sudo systemctl start mijnradar.service
systemctl is-active --quiet mijnradar.timer && echo "   timer actief"
