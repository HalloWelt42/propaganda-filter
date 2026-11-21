"""Aufträge: ein Vergleich dauert Minuten, darum läuft er im Hintergrund und die App fragt den Stand ab.

Es rechnen höchstens `grenzen.gleichzeitig` Aufträge zugleich, die übrigen warten. Fertige Aufträge bleiben eine Weile
abrufbar (`grenzen.auftraege_behalten_s`) und verschwinden dann aus dem Speicher; der Filter merkt sich Aufträge nicht
über einen Neustart (die Signaturen schon).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from .modelle import Auftrag, Ergebnis, Zustand

Arbeit = Callable[[Auftrag], Awaitable[Ergebnis]]


def _jetzt() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Auftragsbuch:
    def __init__(self, gleichzeitig: Callable[[], int], behalten_s: Callable[[], float]) -> None:
        self._gleichzeitig = gleichzeitig
        self._behalten_s = behalten_s
        self._auftraege: dict[str, Auftrag] = {}
        self._ende: dict[str, float] = {}
        self._laeufe: set[asyncio.Task[None]] = set()
        self._laufend = 0
        self._frei = asyncio.Condition()

    def anlegen(self, app: str, quelle: str, arbeit: Arbeit, *, kennung: str | None = None) -> Auftrag:
        self._abraeumen()
        auftrag = Auftrag(id=kennung or str(uuid.uuid4()), app=app, quelle=quelle, angelegt=_jetzt())
        self._auftraege[auftrag.id] = auftrag
        lauf = asyncio.create_task(self._ausfuehren(auftrag, arbeit))
        self._laeufe.add(lauf)
        lauf.add_done_callback(self._laeufe.discard)
        return auftrag

    def holen(self, kennung: str, app: str) -> Auftrag:
        """Nur die App, die den Auftrag angelegt hat, sieht ihn."""
        auftrag = self._auftraege.get(kennung)
        if auftrag is None or auftrag.app.casefold() != app.casefold():
            raise KeyError(kennung)
        return auftrag

    def zahlen(self) -> dict[Zustand, int]:
        zahlen: dict[Zustand, int] = {"wartet": 0, "laeuft": 0, "fertig": 0, "fehler": 0}
        for a in self._auftraege.values():
            zahlen[a.zustand] += 1
        return zahlen

    async def schliessen(self) -> None:
        for lauf in list(self._laeufe):
            lauf.cancel()
        await asyncio.gather(*self._laeufe, return_exceptions=True)

    async def _ausfuehren(self, auftrag: Auftrag, arbeit: Arbeit) -> None:
        async with self._frei:
            await self._frei.wait_for(lambda: self._laufend < self._gleichzeitig())
            self._laufend += 1
        try:
            auftrag.zustand, auftrag.schritt = "laeuft", "beginnt"
            auftrag.ergebnis = await arbeit(auftrag)
            auftrag.zustand, auftrag.schritt, auftrag.fortschritt = "fertig", "fertig", 1.0
        except asyncio.CancelledError:
            auftrag.zustand, auftrag.fehler = "fehler", "abgebrochen (Filter beendet)"
            raise
        except Exception as f:  # jeder Fehler gehört an den Auftrag, nicht in den Hintergrund
            auftrag.zustand, auftrag.fehler = "fehler", str(f) or type(f).__name__
        finally:
            auftrag.beendet = _jetzt()
            self._ende[auftrag.id] = time.monotonic()
            async with self._frei:
                self._laufend -= 1
                self._frei.notify_all()

    def _abraeumen(self) -> None:
        grenze = time.monotonic() - self._behalten_s()
        for kennung in [k for k, t in self._ende.items() if t < grenze]:
            self._ende.pop(kennung)
            self._auftraege.pop(kennung, None)
