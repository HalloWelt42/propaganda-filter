"""Einen Sender eine Weile mitschneiden: Ton in eine Datei, die Titel aus den Sendedaten daneben (Lernlauf, HELF-256)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

TITEL = re.compile(r"StreamTitle='(.*?)';")
VERBINDEN_S, STILLE_S = 20.0, 30.0


class MitschnittFehler(Exception):
    pass


@dataclass
class Titelzeile:
    """Ab dieser Sekunde des Mitschnitts nannte der Sender diesen Titel (leer: keinen)."""

    ab: float
    titel: str


class Sendedaten:
    """Trennt Ton und eingestreute Sendedaten (alle `abstand` Bytes ein Längenbyte und so viele 16er-Blöcke Text)."""

    def __init__(self, abstand: int) -> None:
        self.abstand = abstand
        self._bis_daten = abstand
        self._offen = bytearray()
        self._fehlt = 0

    def nehmen(self, daten: bytes) -> tuple[bytes, list[str]]:
        if not self.abstand:
            return daten, []
        ton, titel, i = bytearray(), [], 0
        while i < len(daten):
            if self._fehlt:                                   # mitten in den Sendedaten
                stueck = daten[i:i + self._fehlt]
                self._offen += stueck
                self._fehlt -= len(stueck)
                i += len(stueck)
                if not self._fehlt:
                    if t := TITEL.search(self._offen.rstrip(b"\0").decode("utf-8", "replace")):
                        titel.append(t[1].strip())
                    self._offen.clear()
            elif self._bis_daten:
                stueck = daten[i:i + self._bis_daten]
                ton += stueck
                self._bis_daten -= len(stueck)
                i += len(stueck)
            else:
                self._fehlt = daten[i] * 16
                self._bis_daten = self.abstand
                i += 1
        return bytes(ton), titel


@dataclass
class Mitschnitt:
    datei: Path
    bytes: int = 0
    begonnen: float = field(default_factory=time.monotonic)
    _titel: list[tuple[int, str]] = field(default_factory=list)      # Bytestelle und Titel

    @property
    def laeuft_s(self) -> float:
        return time.monotonic() - self.begonnen

    @property
    def titel(self) -> str:
        return self._titel[-1][1] if self._titel else ""

    def zeilen(self, dauer: float) -> list[Titelzeile]:
        """Die Titel auf der Zeitachse des dekodierten Tons (gleichmäßige Bitrate)."""
        return [Titelzeile(stelle / self.bytes * dauer, t) for stelle, t in self._titel] if self.bytes else []


async def mitschneiden(m: Mitschnitt, adresse: str, sekunden: float, *, groesse: int,
                       weiter: Callable[[], bool] = lambda: True, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Bis `sekunden` um sind, der Sender endet oder `weiter` nein sagt. `groesse`: mehr Bytes nimmt ein Mitschnitt nicht."""
    try:
        async with (httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(VERBINDEN_S, read=STILLE_S), transport=transport,
                                      headers={"User-Agent": "Propaganda-Filter", "Icy-MetaData": "1"}) as c,
                    c.stream("GET", adresse) as r):
            if r.status_code != 200:
                raise MitschnittFehler(f"der Sender antwortet mit {r.status_code}")
            art = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if "mpegurl" in art or "scpls" in art or art.startswith("text/"):
                raise MitschnittFehler(f"kein durchgehender Ton ({art})")
            daten = Sendedaten(int(r.headers.get("icy-metaint") or 0))
            m.begonnen = time.monotonic()
            with m.datei.open("wb") as f:
                async for teil in r.aiter_bytes(16384):
                    ton, titel = daten.nehmen(teil)
                    f.write(ton)
                    m.bytes += len(ton)
                    m._titel += [(m.bytes, t) for t in titel if t != m.titel or not m._titel]
                    if m.laeuft_s >= sekunden or m.bytes >= groesse or not weiter():
                        return
    except (httpx.HTTPError, ValueError) as f:
        if m.bytes == 0 or not isinstance(f, httpx.HTTPError):
            raise MitschnittFehler(f"nicht erreichbar ({type(f).__name__})") from f
        # bricht der Sender ab, zählt, was bis dahin kam
