"""Lernlauf (HELF-256): im Zeitfenster Sender hören, wiederkehrende Stücke finden und als Vorschläge ablegen.

Welche Sender, sagt die App (mit Grund und Gewicht); der Lauf nimmt die wichtigsten, die am längsten nicht dran waren. Ein
Mitschnitt wird eine Sitzung lang aufgenommen, ausgewertet und verworfen; es bleiben seine Abdrücke (für Vergleiche über
Tage) und die Ausschnitte der Kandidaten. Ein Kandidat wirkt nirgends, bis ein Mensch ihn bestätigt.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
from pydantic import BaseModel, Field

from .einstellungen import Einstellungen
from .fingerabdruck import RATE, SCHRITT, ZEILE_S, abdruecke
from .klang import KlangFehler, dekodieren
from .lernbuch import Grund, Lauf, Lernbuch, Lernsender, jetzt
from .mitschnitt import Mitschnitt, MitschnittFehler, Titelzeile, mitschneiden
from .signaturen import SignaturFehler, Signaturbuch
from .wiederholung import Archiv, Aufnahme, Kandidat, ausduennen, suchen

log = logging.getLogger(__name__)

PRUEFTAKT_S = 15.0
ZU_KURZ_S = 120.0               # ein kürzerer Mitschnitt sagt nichts
MUSIK_AB_S = 100.0              # ein Titel "A - B", der so lange steht, ist ein Lied oder eine Sendung, keine Werbung
TEIL_S = 60                     # in solchen Teilen werden Abdrücke gerechnet
FENSTER_S, FENSTER_SCHRITT_S = 4.0, 2.0


class Laufend(BaseModel):
    quelle: str
    name: str
    grund: Grund
    seit: str
    schritt: str
    sekunden: int
    titel: str


class Lernstand(BaseModel):
    an: bool
    von: str
    bis: str
    gleichzeitig: int
    laeuft: bool = Field(description="gerade wird gehört (Zeitfenster oder von Hand gestartet)")
    sofort_bis: str | None = Field(None, description="von Hand gestartet, bis dahin")
    laufend: list[Laufend]
    sender: int
    stunden: float
    archiv_stunden: float
    naechste: list[str]
    laeufe: list[Lauf]


@dataclass
class _Horcher:
    sender: Lernsender
    ordner: Path
    mitschnitt: Mitschnitt
    seit: datetime = field(default_factory=jetzt)
    schritt: str = "verbindet"


def im_fenster(uhr: str, von: str, bis: str) -> bool:
    """Liegt die Uhrzeit im Zeitfenster, auch über Mitternacht?"""
    return von <= uhr < bis if von < bis else uhr >= von or uhr < bis


def ist_musik(k: Kandidat, titel: list[Titelzeile], dauer: float) -> bool:
    """Liegt der Kandidat größtenteils in einem Titel, der so lange steht, dass es ein Lied oder eine Sendung ist?"""
    enden = [z.ab for z in titel[1:]] + [dauer]
    return any(" - " in z.titel and bis - z.ab >= MUSIK_AB_S and min(k.bis, bis) - max(k.von, z.ab) > 0.6 * k.dauer
               for z, bis in zip(titel, enden))


def abdruecke_lang(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Abdrücke eines langen Tons, in Teilen gerechnet (gleichbleibender Speicher)."""
    h, t, n = [], [], TEIL_S * RATE // SCHRITT * SCHRITT
    for ab in range(0, len(x), n):
        hh, tt = abdruecke(x[ab:ab + n + 2 * RATE])
        m = tt < n // SCHRITT
        h.append(hh[m])
        t.append(tt[m] + ab // SCHRITT)
    return (np.concatenate(h), np.concatenate(t)) if h else (np.empty(0, np.int64), np.empty(0, np.int64))


class Lernlauf:
    def __init__(self, ordner: Path, buch: Lernbuch, signaturen: Signaturbuch, einstellungen: Callable[[], Einstellungen],
                 transport: httpx.AsyncBaseTransport | None = None, bei_ende: Callable[[], None] = lambda: None) -> None:
        """`bei_ende`: ein Lauf ist abgeschlossen und hat alles abgelegt."""
        self._bei_ende = bei_ende
        self.buch, self.signaturen, self._e, self._transport = buch, signaturen, einstellungen, transport
        self.arbeit = ordner / "arbeit"
        shutil.rmtree(self.arbeit, ignore_errors=True)
        self.arbeit.mkdir(parents=True)
        self._horcher: dict[str, _Horcher] = {}
        self._aufgaben: set[asyncio.Task[None]] = set()
        self._auswerten = asyncio.Lock()
        self._lauf: str | None = None
        self._takt: asyncio.Task[None] | None = None
        self._sofort_bis: datetime | None = None

    # ----- von außen

    def starten(self) -> None:
        self._takt = asyncio.create_task(self._schleife())

    async def schliessen(self) -> None:
        alle = [*self._aufgaben, *([self._takt] if self._takt else [])]
        for a in alle:
            a.cancel()
        await asyncio.gather(*alle, return_exceptions=True)

    def jetzt_laufen(self, minuten: float) -> None:
        """Auch außerhalb des Zeitfensters hören; 0 beendet das wieder."""
        self._sofort_bis = datetime.fromtimestamp(time.time() + minuten * 60).astimezone() if minuten else None
        self.pruefen()

    def soll_laufen(self) -> bool:
        e = self._e().lernlauf
        sofort = self._sofort_bis is not None and jetzt() < self._sofort_bis
        return sofort or (e.an and im_fenster(jetzt().strftime("%H:%M"), e.von, e.bis))

    def stand(self) -> Lernstand:
        e, z = self._e().lernlauf, self.buch.zahlen()
        sofort = self._sofort_bis if self._sofort_bis and jetzt() < self._sofort_bis else None
        return Lernstand(
            an=e.an, von=e.von, bis=e.bis, gleichzeitig=e.gleichzeitig, laeuft=bool(self._horcher) or self.soll_laufen(),
            sofort_bis=sofort.isoformat(timespec="seconds") if sofort else None, sender=z.sender, stunden=z.stunden,
            archiv_stunden=z.archiv_stunden, laeufe=self.buch.laeufe(), naechste=[s.name for s in self.buch.naechste(8, set(self._horcher))],
            laufend=[Laufend(quelle=h.sender.quelle, name=h.sender.name, grund=h.sender.grund, seit=h.seit.isoformat(timespec="seconds"),
                             schritt=h.schritt, sekunden=round(h.mitschnitt.laeuft_s), titel=h.mitschnitt.titel) for h in self._horcher.values()])

    # ----- Ablauf

    async def _schleife(self) -> None:
        while True:
            try:
                self.pruefen()
            except Exception:
                log.exception("Lernlauf")
            await asyncio.sleep(PRUEFTAKT_S)

    def pruefen(self) -> None:
        """Freie Plätze besetzen, oder den Lauf abschließen."""
        if self.soll_laufen():
            for s in self.buch.naechste(self._e().lernlauf.gleichzeitig - len(self._horcher), set(self._horcher)):
                self._lauf = self._lauf or self.buch.lauf_beginnen()
                ordner = self.arbeit / str(uuid.uuid4())
                ordner.mkdir()
                self._horcher[s.quelle] = h = _Horcher(s, ordner, Mitschnitt(ordner / "ton"))
                a = asyncio.create_task(self._hoeren(h, self._lauf))
                self._aufgaben.add(a)
                a.add_done_callback(self._aufgaben.discard)
        if self._lauf and not self._horcher:
            self.buch.lauf_beenden(self._lauf)
            self.buch.vergessen(self._e().lernlauf.archiv_h)
            self._lauf = None
            self._bei_ende()

    async def _hoeren(self, h: _Horcher, lauf: str) -> None:
        e = self._e().lernlauf
        try:
            h.schritt = "hört"
            await mitschneiden(h.mitschnitt, h.sender.adresse, e.sitzung_min * 60, groesse=e.sitzung_min * 60 * e.kbit_hoechstens * 125,
                               weiter=self.soll_laufen, transport=self._transport)
            h.schritt = "wartet auf die Auswertung"
            async with self._auswerten:
                h.schritt = "wird ausgewertet"
                dauer, neu, bekannte = await asyncio.to_thread(self._werten, h.sender, h.mitschnitt, lauf)
            self.buch.gehoert(lauf, h.sender.quelle, dauer, neu, bekannte)
        except (MitschnittFehler, KlangFehler) as f:
            self.buch.gescheitert(lauf, h.sender.quelle, str(f))
        except Exception as f:
            log.exception("Lernlauf %s", h.sender.name)
            self.buch.gescheitert(lauf, h.sender.quelle, type(f).__name__)
        finally:
            shutil.rmtree(h.ordner, ignore_errors=True)
            self._horcher.pop(h.sender.quelle, None)

    # ----- Auswertung (eigener Faden)

    def _werten(self, s: Lernsender, m: Mitschnitt, lauf: str) -> tuple[float, int, int]:
        """(Dauer, neue Kandidaten, schon bekannte Werbung)"""
        e = self._e()
        x = dekodieren(m.datei)
        dauer = len(x) / RATE
        if dauer < ZU_KURZ_S:
            raise MitschnittFehler(f"nur {dauer:.0f} s Ton")
        h, t = abdruecke_lang(x)
        bekannt = self._bekannte(h, t, s)
        ich = Aufnahme(str(uuid.uuid4()), s.quelle, dauer, *ausduennen(h, t))
        kandidaten = suchen(ich, Archiv([*self.buch.archiv(), ich]), mindestens_s=e.lernlauf.mindestens_s, hoechstens_s=e.lernlauf.hoechstens_s)
        titel, neu = m.zeilen(dauer), 0
        for k in kandidaten:
            if ist_musik(k, titel, dauer) or any(min(k.bis, b) - max(k.von, a) > 0.5 * k.dauer for a, b in bekannt):
                continue
            neu += self._ablegen(s, m.datei, k, lauf)
        self.buch.merken(ich, s.name)
        return dauer, neu, len(bekannt)

    def _bekannte(self, h: np.ndarray, t: np.ndarray, s: Lernsender) -> list[tuple[float, float]]:
        """Wo schon bekannte Werbung lief; zählt als Treffer an diesem Sender."""
        e, v = self._e().wiedererkennen, self.signaturen.verzeichnis()
        laengen = dict(zip(v.spots, v.laengen))
        treffer: dict[tuple[str, int], int] = {}
        fenster, schritt = int(FENSTER_S / ZEILE_S), int(FENSTER_SCHRITT_S / ZEILE_S)
        for ab in range(0, int(t[-1]) if len(t) else 0, schritt):
            a, b = np.searchsorted(t, ab), np.searchsorted(t, ab + fenster)
            for f in v.suchen(h[a:b], t[a:b], mindestens=e.punkte_sicher, anteil=e.anteil_sicher):
                schl = (f.spot, round(f.beginn))
                schl = next((k for k in ((f.spot, round(f.beginn) + d) for d in (0, -1, 1)) if k in treffer), schl)
                treffer[schl] = treffer.get(schl, 0) + 1
        aus = []
        for (spot, beginn), n in treffer.items():
            if n * FENSTER_SCHRITT_S >= 0.5 * (laengen[spot] - FENSTER_S):        # der größte Teil lief, nicht nur ein gemeinsamer Jingle
                self.signaturen.treffer(spot, s.quelle, s.name)
                aus.append((float(beginn), beginn + laengen[spot]))
        return aus

    def _ablegen(self, s: Lernsender, ton: Path, k: Kandidat, lauf: str) -> bool:
        try:
            kennung = self.signaturen.gleiche(ton, (k.von, k.bis), self._e().wiedererkennen)
            neu = kennung is None
            if kennung is None:
                kennung = self.signaturen.aufnehmen(ton, k.von, k.bis, quelle=s.name, app="", herkunft="lernlauf", fassung="",
                                                    auftrag=lauf, quelle_kennung=s.quelle).id
        except (SignaturFehler, KlangFehler) as f:
            log.warning("Kandidat %s %.1f: %s", s.name, k.von, f)
            return False
        self.signaturen.treffer(kennung, s.quelle, s.name)
        for quelle in (v.quelle for v in k.vorkommen):
            self.signaturen.treffer(kennung, quelle, self.buch.name(quelle))
        return neu
