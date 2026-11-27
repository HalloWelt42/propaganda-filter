"""Kandidaten mithören (HELF-256): was wird gesprochen, und klingt es nach Werbung? Beides rechnet die Leitstelle, immer als
Hintergrundarbeit. Es ordnet die Durchsicht; ist die Einordnung sicher (Werbung mit erkanntem Werbenden), gibt die Regel der
Freigabe die Signatur selbst frei (RAD-154, `[freigabe]`), sonst entscheidet der Mensch.

Gearbeitet wird in Bündeln (HELF-265): erst wenn genug wartet, etwas lange genug wartet oder ein Lernlauf zu Ende ist. Dann wird
erst alles gehört und danach alles eingeordnet, ohne Pause dazwischen. So lädt die Leitstelle das Sprachmodell je Bündel einmal,
statt es zwischen einzelnen Stücken zu entladen und wieder zu laden. Was bricht, wird beim nächsten Mal fortgesetzt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import httpx
from appkern.leitstelle import Leitstelle, LeitstellenFehler, Nachricht, als_masse, antwortform, gerade, hoeren, json_lesen
from appkern.leitstelle.arbeit import LAUFENDE_ARBEIT
from pydantic import BaseModel, ValidationError

from .einstellungen import Einstellungen
from .signaturen import Signatur, Signaturbuch
from .tls import pruefung

log = logging.getLogger(__name__)

TAKT_S, NACH_FEHLER_S, HOECHSTENS = 60.0, 600.0, 500
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
        self._gleich = False
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

    def anstossen(self) -> None:
        """Ein Lernlauf ist zu Ende: was da ist, gilt als volles Bündel."""
        self._gleich = True

    def faellig(self, jetzt: datetime | None = None) -> list[Signatur]:
        """Das Bündel, das jetzt dran ist (leer: noch sammeln)."""
        offen = self.signaturen.ohne_einordnung(HOECHSTENS)
        if not offen:
            return []
        e = self._e().leitstelle
        wartet_min = ((jetzt or datetime.now(UTC)) - datetime.fromisoformat(min(s.angelegt for s in offen))).total_seconds() / 60
        return offen if self._gleich or len(offen) >= e.buendel or wartet_min >= e.spaetestens_min else []

    async def _schleife(self) -> None:
        while True:
            pause = TAKT_S
            if self.eingerichtet:
                try:
                    if buendel := self.faellig():
                        await self.rechnen(buendel)
                    self.fehler, self._gleich = "", False
                except (LeitstellenFehler, httpx.HTTPError, OSError) as f:
                    self.fehler, pause = str(f) or type(f).__name__, NACH_FEHLER_S
                except Exception as f:
                    log.exception("Abschrift")
                    self.fehler, pause = type(f).__name__, NACH_FEHLER_S
            await asyncio.sleep(pause)

    async def rechnen(self, buendel: list[Signatur]) -> None:
        """Erst alles hören, dann alles einordnen: je Modell einmal laden."""
        e = self._e()
        arbeit = LAUFENDE_ARBEIT.set(f"abschrift-{uuid4().hex[:12]}")
        try:
            with als_masse():
                async with httpx.AsyncClient(transport=self._transport, verify=pruefung(e.kellerwolke.zertifikatsstelle)) as client:
                    ls = Leitstelle(e.leitstelle.adresse, e.leitstelle.schluessel, "Propaganda-Filter", client=client)
                    texte = [(s.id, s.text or await self._hoeren(ls, e.leitstelle.hoerdienst, s)) for s in buendel]
                    for kennung, text in texte:
                        if text:
                            art, werbender = await self._einordnen(ls, e.leitstelle.modell, text)
                            self.signaturen.text_setzen(kennung, text, art, werbender)
                            if self.signaturen.selbst_freigeben(kennung, e.freigabe):
                                log.info("Signatur %s selbst freigegeben (%s)", kennung, self.signaturen.eine(kennung).werbender)
        finally:
            LAUFENDE_ARBEIT.reset(arbeit)

    async def _hoeren(self, ls: Leitstelle, dienst: str, s: Signatur) -> str:
        """Die Abschrift, gleich gemerkt; leer, wenn nichts Verständliches gesprochen wird (dann ist das Stück fertig)."""
        erg = await hoeren(ls, dienst, self.signaturen.tondatei(s.id), "auto", titel=f"Stück von {s.quelle}", prioritaet="hintergrund")
        text = gerade(erg.text.strip())
        genug = len(text) >= MINDESTENS_ZEICHEN
        self.signaturen.text_setzen(s.id, text, "" if genug else "ohne_sprache", "")
        return text if genug else ""

    @staticmethod
    async def _einordnen(ls: Leitstelle, modell: str, text: str) -> tuple[Art, str]:
        if not modell:
            return "unklar", ""
        antwort = await ls.chat([Nachricht.system(ANWEISUNG), Nachricht.nutzer(text[:4000])], modell=modell, titel="Einordnen",
                                prioritaet="hintergrund", temperature=0, response_format=FORM)
        try:
            e = Einordnung.model_validate(json_lesen(antwort.text))
        except (ValueError, ValidationError):
            return "unklar", ""
        return e.art, gerade(e.werbender.strip())[:200] if e.art == "werbung" else ""

