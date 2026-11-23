"""Kandidaten mithören (HELF-256): was wird gesprochen, und klingt es nach Werbung? Beides rechnet die Leitstelle, immer als
Hintergrundarbeit. Es ordnet nur die Durchsicht; ob etwas wirkt, entscheidet weiter der Mensch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Literal

import httpx
from appkern.leitstelle import Leitstelle, LeitstellenFehler, Nachricht, antwortform, gerade, hoeren, json_lesen
from pydantic import BaseModel, ValidationError

from .einstellungen import Einstellungen
from .signaturen import Signaturbuch
from .tls import pruefung

log = logging.getLogger(__name__)

TAKT_S, NACH_FEHLER_S, JE_RUNDE = 60.0, 600.0, 10
MINDESTENS_ZEICHEN = 12

Art = Literal["werbung", "sender", "anderes", "ohne_sprache", "unklar"]

ANWEISUNG = """Du bekommst die Abschrift eines kurzen Tonstücks, das im Radio mehrmals unverändert lief. Ordne es ein:
- "werbung": wirbt für ein Produkt, eine Firma, ein Angebot, eine Veranstaltung oder eine App von außerhalb des Senders
- "sender": der Sender über sich selbst (Kennung, Programmhinweis, Gewinnspiel, eigene App)
- "anderes": Nachrichten, Wetter, Verkehr, Moderation, Liedtext, oder nicht zu entscheiden
"werbender" ist bei Werbung der kurze Name der Firma oder Marke, sonst leer. Die Abschrift kann Hörfehler enthalten."""
FORM = antwortform("einordnung", {
    "type": "object", "additionalProperties": False, "required": ["art", "werbender"],
    "properties": {"art": {"type": "string", "enum": ["werbung", "sender", "anderes"]}, "werbender": {"type": "string"}}})


class Einordnung(BaseModel):
    art: Literal["werbung", "sender", "anderes"]
    werbender: str = ""


class Abschrift:
    def __init__(self, signaturen: Signaturbuch, einstellungen: Callable[[], Einstellungen],
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.signaturen, self._e, self._transport = signaturen, einstellungen, transport
        self.fehler = ""
        self._aufgabe: asyncio.Task[None] | None = None

    @property
    def eingerichtet(self) -> bool:
        return bool(self._e().leitstelle.schluessel)

    def starten(self) -> None:
        self._aufgabe = asyncio.create_task(self._schleife())

    async def schliessen(self) -> None:
        if self._aufgabe:
            self._aufgabe.cancel()
            await asyncio.gather(self._aufgabe, return_exceptions=True)

    async def _schleife(self) -> None:
        while True:
            pause = TAKT_S
            if self.eingerichtet:
                try:
                    await self.runde()
                    self.fehler = ""
                except (LeitstellenFehler, httpx.HTTPError, OSError) as f:
                    self.fehler, pause = str(f) or type(f).__name__, NACH_FEHLER_S
                except Exception as f:
                    log.exception("Abschrift")
                    self.fehler, pause = type(f).__name__, NACH_FEHLER_S
            await asyncio.sleep(pause)

    async def runde(self) -> int:
        """Die neuesten Stücke ohne Abschrift; liefert, wie viele."""
        offen = self.signaturen.ohne_text(JE_RUNDE)
        if not offen:
            return 0
        e = self._e()
        async with httpx.AsyncClient(transport=self._transport, verify=pruefung(e.kellerwolke.zertifikatsstelle)) as client:
            ls = Leitstelle(e.leitstelle.adresse, e.leitstelle.schluessel, "Propaganda-Filter", client=client)
            for s in offen:
                erg = await hoeren(ls, e.leitstelle.hoerdienst, self.signaturen.tondatei(s.id), "auto", titel=f"Stück von {s.quelle}",
                                   prioritaet="hintergrund")
                text = gerade(erg.text.strip())
                art, werbender = await self._einordnen(ls, e.leitstelle.modell, text)
                self.signaturen.text_setzen(s.id, text, art, werbender)
        return len(offen)

    @staticmethod
    async def _einordnen(ls: Leitstelle, modell: str, text: str) -> tuple[Art, str]:
        if len(text) < MINDESTENS_ZEICHEN:
            return "ohne_sprache", ""
        if not modell:
            return "unklar", ""
        antwort = await ls.chat([Nachricht.system(ANWEISUNG), Nachricht.nutzer(text[:4000])], modell=modell, titel="Einordnen",
                                prioritaet="hintergrund", temperature=0, response_format=FORM)
        try:
            e = Einordnung.model_validate(json_lesen(antwort.text))
        except (ValueError, ValidationError):
            return "unklar", ""
        return e.art, gerade(e.werbender.strip())[:200] if e.art == "werbung" else ""

