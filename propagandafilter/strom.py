"""Einen laufenden Strom auf bekannte Spots prüfen (HELF-256).

Die App reicht den Ton so weiter, wie er vom Sender kommt (MP3 oder AAC), Stück für Stück und lückenlos; ein FFmpeg je Strom
macht daraus 8 kHz. Geprüft wird alle `TAKT_S` das letzte `FENSTER_S` lange Stück. Ein Spot gilt als gefunden, wenn er
deutlich auffällt oder zwei Fenster nacheinander denselben Beginn nennen; seine Stelle reicht von seinem Beginn über die
ganze bekannte Länge. Bricht er früher ab (der Sender blendet aus), endet die Stelle dort.

Zeiten sind Sekunden des Stroms, wie die App sie nennt (`ab` beim ersten Stück).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .einstellungen import Wiedererkennen
from .fingerabdruck import RATE, SCHRITT, Treffer, Verzeichnis, abdruecke

Art = Literal["mp3", "aac"]
FENSTER_S = 4.0
TAKT_S = 0.5
GLEICHER_BEGINN_S = 0.25       # so weit dürfen zwei Fenster beim Beginn desselben Spots auseinanderliegen
VERLOREN_FENSTER = 4           # nach so vielen Fenstern ohne den Spot gilt er als abgebrochen
SPAETESTENS_S = 5.0            # so spät nach seinem Beginn darf ein Spot zum ersten Mal auffallen (sonst ist es nur ein gemeinsames
                               # Stück, etwa der Jingle des Senders am Ende eines Spots); beim Verbinden mittendrin gilt das nicht
NACHLAUF_S = 600.0             # so lange bleiben gefundene Stellen abrufbar (der Zeitversatz reicht weiter zurück, die App merkt sie sich)


class StromFehler(Exception):
    pass


@dataclass
class Fund:
    """Ein Spot im Strom (Sekunden des Stroms)."""

    spot: str
    von: float
    bis: float
    punkte: int
    anteil: float
    sicher_ab: float               # Stelle des Tons, an der die Erkennung feststand
    beginn: float                  # wo der Spot anfing, auch wenn das vor dem Verbinden lag (von beginnt dann dort)
    abgebrochen: bool = False
    verloren: int = 0


@dataclass
class _Verdacht:
    beginn: float
    punkte: int
    fenster_ab: float              # das erste Fenster, in dem er auffiel


@dataclass
class Strom:
    """Eine Sitzung; `name` ist, was die App als Quelle nennt (Sender)."""

    app: str
    quelle: str
    name: str
    art: Art
    einstellungen: Callable[[], Wiedererkennen]
    verzeichnis: Callable[[], Verzeichnis]
    bei_fund: Callable[[Strom, Fund], None]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    funde: list[Fund] = field(default_factory=list)
    zuletzt: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self._prozess: asyncio.subprocess.Process | None = None
        self._leser: asyncio.Task[None] | None = None
        self._ton = np.empty(0, np.float32)
        self._ton_ab = 0               # Nummer der ersten Probe in _ton
        self._proben = 0               # so viele Proben kamen insgesamt
        self._geprueft = 0             # bis zu dieser Probe ist geprüft
        self._rest = b""
        self.ab: float | None = None
        self._verdacht: dict[str, _Verdacht] = {}

    @property
    def geprueft_bis(self) -> float:
        return (self.ab or 0.0) + self._geprueft / RATE

    async def nehmen(self, daten: bytes, ab: float) -> None:
        """Ein Stück Ton; `ab` ist die Stelle des ersten Stücks, danach zählt der Ton selbst."""
        self.zuletzt = time.monotonic()
        if self._prozess is None:
            self.ab = ab
            self._prozess = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-probesize", "4096", "-analyzeduration", "0",
                "-f", self.art, "-i", "pipe:0", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-flush_packets", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self._leser = asyncio.create_task(self._lesen())
        if self._prozess.returncode is not None:
            raise StromFehler("Der Ton ließ sich nicht lesen")
        assert self._prozess.stdin is not None
        try:
            self._prozess.stdin.write(daten)
            await self._prozess.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as f:
            raise StromFehler("Der Ton ließ sich nicht lesen") from f
        await asyncio.sleep(0)

    async def _lesen(self) -> None:
        assert self._prozess is not None and self._prozess.stdout is not None
        while teil := await self._prozess.stdout.read(1 << 15):
            teil = self._rest + teil
            ganz = len(teil) // 4 * 4
            self._rest = teil[ganz:]
            neu = np.frombuffer(teil[:ganz], np.float32)
            self._ton = np.concatenate([self._ton, neu])
            self._proben += len(neu)
            await self._pruefen()

    async def _pruefen(self) -> None:
        fenster, takt = int(FENSTER_S * RATE), int(TAKT_S * RATE)
        while self._proben - max(self._geprueft, fenster) >= (takt if self._geprueft >= fenster else 0) and self._proben >= fenster:
            ende = fenster if self._geprueft < fenster else self._geprueft + takt
            a = ende - fenster - self._ton_ab
            x = self._ton[a:a + fenster]
            self._geprueft = ende
            v = self.verzeichnis()
            treffer = await asyncio.to_thread(self._suchen, x, v)
            self._werten(treffer, v, (ende - fenster) / RATE)
            vergessen = ende - fenster + takt - self._ton_ab
            if vergessen > 8 * takt:
                self._ton = self._ton[vergessen:]
                self._ton_ab += vergessen

    def _suchen(self, x: np.ndarray, v: Verzeichnis) -> list[Treffer]:
        e = self.einstellungen()
        h, t = abdruecke(x)
        return v.suchen(h, t, mindestens=e.punkte_verdacht, anteil=e.anteil_verdacht)

    def _werten(self, treffer: list[Treffer], v: Verzeichnis, fenster_ab: float) -> None:
        """Treffer eines Fensters, das bei `fenster_ab` (Sekunden seit dem ersten Stück) beginnt."""
        e, null = self.einstellungen(), self.ab or 0.0
        fenster_bis = null + fenster_ab + FENSTER_S
        je = {x.spot: x for x in treffer}
        for f in self.funde:
            # läuft der Spot noch? erst prüfen, wenn das Fenster ganz in ihm liegt
            if f.abgebrochen or not (f.von <= null + fenster_ab and fenster_bis <= f.bis):
                continue
            x = je.get(f.spot)
            f.verloren = 0 if x and abs(null + fenster_ab + x.beginn - f.beginn) <= GLEICHER_BEGINN_S else f.verloren + 1
            if f.verloren >= VERLOREN_FENSTER:
                f.abgebrochen = True
                f.bis = max(f.von, fenster_bis - FENSTER_S - (f.verloren - 1) * TAKT_S)
        neu: dict[str, _Verdacht] = {}
        for x in treffer:
            beginn = null + fenster_ab + x.beginn
            if any(f.spot == x.spot and abs(f.beginn - beginn) <= GLEICHER_BEGINN_S for f in self.funde):
                continue
            vorher = self._verdacht.get(x.spot)
            fest = (x.punkte >= e.punkte_sicher and x.anteil >= e.anteil_sicher) or (
                vorher is not None and abs(vorher.beginn - beginn) <= GLEICHER_BEGINN_S)
            if not fest:
                neu[x.spot] = _Verdacht(beginn, x.punkte, fenster_ab)
                continue
            erstes = vorher.fenster_ab if vorher and abs(vorher.beginn - beginn) <= GLEICHER_BEGINN_S else fenster_ab
            if null + erstes - beginn > SPAETESTENS_S and erstes > 0:
                neu[x.spot] = _Verdacht(beginn, x.punkte, erstes)      # merken, damit auch die folgenden Fenster es nicht neu behaupten
                continue
            dauer = v.laengen[v.spots.index(x.spot)]
            # dasselbe Stück Ton steht oft zweimal im Verzeichnis (derselbe Spot von zwei Sendern): eine Stelle genügt
            fund = Fund(x.spot, max(null, beginn), beginn + dauer, x.punkte, round(x.anteil, 3), fenster_bis, beginn)
            if any(not f.abgebrochen and f.von - 1 <= fund.von and fund.bis <= f.bis + 1 for f in self.funde):
                continue
            self.funde.append(fund)
            self.bei_fund(self, fund)
        self._verdacht = neu
        self.funde = [f for f in self.funde if f.bis >= fenster_bis - NACHLAUF_S]

    async def schliessen(self) -> None:
        if self._leser:
            self._leser.cancel()
        if self._prozess and self._prozess.returncode is None:
            self._prozess.kill()
            await self._prozess.wait()


class Stroeme:
    """Die offenen Sitzungen; wer eine Weile nichts schickt, ist weg."""

    def __init__(self, hoechstens: Callable[[], int], stille_s: Callable[[], float]) -> None:
        self._hoechstens, self._stille_s = hoechstens, stille_s
        self._offen: dict[str, Strom] = {}

    async def anlegen(self, strom: Strom) -> Strom:
        await self._abraeumen()
        if len(self._offen) >= self._hoechstens():
            raise StromFehler("Zu viele Ströme zugleich")
        self._offen[strom.id] = strom
        return strom

    def holen(self, kennung: str, app: str) -> Strom:
        s = self._offen.get(kennung)
        if s is None or s.app.casefold() != app.casefold():
            raise KeyError(kennung)
        return s

    async def beenden(self, kennung: str) -> None:
        if s := self._offen.pop(kennung, None):
            await s.schliessen()

    async def _abraeumen(self) -> None:
        grenze = time.monotonic() - self._stille_s()
        for k in [k for k, s in self._offen.items() if s.zuletzt < grenze]:
            await self.beenden(k)

    def alle(self) -> list[Strom]:
        return list(self._offen.values())

    async def schliessen(self) -> None:
        for k in list(self._offen):
            await self.beenden(k)
