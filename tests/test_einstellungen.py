"""Einstellungen: Vorgaben beim ersten Start, ungültige Änderungen werden übergangen."""

import os

from propagandafilter.einstellungen import Einstellungsquelle


def test_vorgaben_und_ungueltige_aenderung(tmp_path):
    datei = tmp_path / "einstellungen.toml"
    q = Einstellungsquelle(datei)
    assert q.aktuell().app_erlaubt("RadioHub") and q.aktuell().kellerwolke.adresse.startswith("https://")
    datei.write_text(datei.read_text().replace('erlaubte_apps = [\n    "radiohub",\n]', "erlaubte_apps = []"))
    os.utime(datei, ns=(1, 1))
    assert q.aktuell().app_erlaubt("radiohub")  # die letzte gültige gilt weiter
    assert q.fehler and "leer" in q.fehler
