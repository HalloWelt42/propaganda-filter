"""Zusammenbau: Datenordner, Einstellungen, Prüfer, Signaturen, Vergleicher, Aufträge und Schnittstelle.

Start: `.venv/bin/uvicorn --factory propagandafilter.main:app_bauen --host 127.0.0.1 --port 8572` (so trägt ihn das
Stellwerk ein; von außen nur über den Vorbau des Macs). Der Datenordner kommt aus PROPAGANDAFILTER_DATEN (Vorgabe
~/Library/Application Support/Propaganda-Filter).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from .api import fehler_einhaengen, schnittstelle
from .auftraege import Auftragsbuch
from .einstellungen import Einstellungsquelle
from .fassung import fassung
from .kellerwolke import KellerwolkePruefer
from .lernbuch import Lernbuch
from .lernlauf import Lernlauf
from .signaturen import Signaturbuch
from .strom import Stroeme
from .vergleich import Vergleicher

VORGABE_DATEN = Path("~/Library/Application Support/Propaganda-Filter")


def datenordner() -> Path:
    return Path(os.path.realpath(Path(os.environ.get("PROPAGANDAFILTER_DATEN", str(VORGABE_DATEN))).expanduser()))


def app_bauen(daten: Path | None = None, *, kellerwolke: httpx.AsyncBaseTransport | None = None,
              anbieter: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """`kellerwolke` und `anbieter` ersetzen in Tests die Verbindungen zur Kellerwolke und zu Anbietern und Sendern."""
    daten = daten or datenordner()
    daten.mkdir(parents=True, exist_ok=True, mode=0o700)
    quelle = Einstellungsquelle(daten / "einstellungen.toml")
    pruefer = KellerwolkePruefer(lambda: quelle.aktuell().kellerwolke, transport=kellerwolke)
    signaturen = Signaturbuch(daten / "signaturen")
    arbeit = daten / "arbeit"
    for rest in arbeit.glob("*"):  # Arbeitsdateien abgebrochener Aufträge (Aufträge überleben keinen Neustart)
        Vergleicher.wegraeumen_ordner(rest)
    vergleicher = Vergleicher(arbeit, signaturen, quelle.aktuell, transport=anbieter)
    stroeme = Stroeme(lambda: quelle.aktuell().wiedererkennen.stroeme_hoechstens, lambda: quelle.aktuell().wiedererkennen.stille_s)
    buch = Lernbuch(daten / "lernlauf")
    lernlauf = Lernlauf(daten / "lernlauf", buch, signaturen, quelle.aktuell, transport=anbieter)
    auftraege = Auftragsbuch(lambda: quelle.aktuell().grenzen.gleichzeitig,
                             lambda: quelle.aktuell().grenzen.auftraege_behalten_s)

    @asynccontextmanager
    async def lebenslauf(_app: FastAPI) -> AsyncIterator[None]:
        lernlauf.starten()
        yield
        await lernlauf.schliessen()
        await stroeme.schliessen()
        await auftraege.schliessen()
        await pruefer.schliessen()
        buch.schliessen()
        signaturen.schliessen()

    app = FastAPI(title="Propaganda-Filter", version=fassung(), lifespan=lebenslauf,
                  description="Findet Werbung in Ton: vergleicht zwei Fassungen derselben Folge und hebt die "
                              "eingesetzten Stellen als Signaturen auf.")
    fehler_einhaengen(app)
    app.include_router(schnittstelle(quelle, pruefer, auftraege, vergleicher, signaturen, stroeme, lernlauf))
    return app
