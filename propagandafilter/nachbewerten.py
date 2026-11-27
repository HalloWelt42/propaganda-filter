"""Einmalige Nachbewertung (RAD-154): die Regel der Freigabe auf die offenen Signaturen anwenden, die vor ihr eingeordnet wurden.
Neue Funde gibt die Abschrift gleich nach der Einordnung frei; das hier holt nur den Bestand nach.

    .venv/bin/python -m propagandafilter.nachbewerten              # Vorschau: welche würden freigegeben
    .venv/bin/python -m propagandafilter.nachbewerten --anwenden   # freigeben

Datenordner wie beim Dienst (PROPAGANDAFILTER_DATEN, sonst die Vorgabe). Es gilt `[freigabe]` aus dessen Einstellungen.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .einstellungen import Einstellungsquelle
from .main import datenordner
from .signaturen import Signatur, Signaturbuch


def zeile(s: Signatur) -> str:
    return f"{s.id}  {s.herkunft:<9} {s.dauer_s:5.1f} s  {s.werbender}  ({s.quelle})"


def ausfuehren(daten: Path, anwenden: bool) -> int:
    einstellungen = daten / "einstellungen.toml"
    if not einstellungen.is_file():
        print(f"Keine Einstellungen unter {einstellungen}", file=sys.stderr)
        return 2
    regel = Einstellungsquelle(einstellungen).aktuell().freigabe
    if not regel.selbst_bei_sicherem_fund:
        print("Die Regel ist aus ([freigabe] selbst_bei_sicherem_fund = false), nichts zu tun")
        return 0
    buch = Signaturbuch(daten / "signaturen")
    try:
        sichere = buch.nachbewerten(regel, anwenden=anwenden)
    finally:
        buch.schliessen()
    for s in sichere:
        print(zeile(s))
    if anwenden:
        print(f"{len(sichere)} Signaturen selbst freigegeben")
    else:
        print(f"{len(sichere)} Signaturen würden freigegeben; mit --anwenden ausführen")
    return 0


def main() -> None:
    befehl = argparse.ArgumentParser(prog="python -m propagandafilter.nachbewerten", description=__doc__.split("\n")[0])
    befehl.add_argument("--anwenden", action="store_true", help="freigeben (ohne: nur Vorschau)")
    sys.exit(ausfuehren(datenordner(), befehl.parse_args().anwenden))


if __name__ == "__main__":
    main()
