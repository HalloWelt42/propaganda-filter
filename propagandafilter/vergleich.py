"""Ein Vergleich zweier Fassungen als Ablauf: zweite Fassung holen, gleich in Bytes?, sonst am Klang ausrichten (beide
Richtungen), verwertbar?, die eingesetzten Stellen als Signaturen aufheben.

Fassung A reicht die App ein (ihre gespeicherte Datei, nur für die gelten die Zeiten); Fassung B holt der Filter selbst
von derselben Adresse beim Anbieter, so wie ein zweiter Abruf sie bekommt. Die Arbeitsdateien liegen je Auftrag in
einem eigenen Ordner und werden danach entfernt.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path

import httpx

from . import klang
from .einstellungen import Einstellungen
from .modelle import Auftrag, Ergebnis, Fundstelle
from .signaturen import SignaturFehler, Signaturbuch


class AbrufFehler(Exception):
    pass


def _pruefsumme(pfad: Path) -> bytes:
    h = hashlib.sha256()
    with pfad.open("rb") as f:
        while teil := f.read(1 << 20):
            h.update(teil)
    return h.digest()


class Vergleicher:
    def __init__(self, arbeitsordner: Path, signaturen: Signaturbuch, einstellungen: Callable[[], Einstellungen], *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.arbeitsordner = arbeitsordner
        self.signaturen = signaturen
        self._einstellungen = einstellungen
        self._transport = transport

    def ordner(self, auftrag: str) -> Path:
        o = self.arbeitsordner / auftrag
        o.mkdir(parents=True, exist_ok=True)
        return o

    def wegraeumen(self, auftrag: str) -> None:
        self.wegraeumen_ordner(self.arbeitsordner / auftrag)

    @staticmethod
    def wegraeumen_ordner(ordner: Path) -> None:
        shutil.rmtree(ordner, ignore_errors=True)

    async def ausfuehren(self, auftrag: Auftrag, zweite: str) -> Ergebnis:
        """Fassung A liegt schon im Ordner des Auftrags (die Schnittstelle hat sie angenommen)."""
        ordner = self.ordner(auftrag.id)
        try:
            return await self._vergleich(auftrag, ordner / "a", ordner / "b", zweite)
        finally:
            self.wegraeumen(auftrag.id)

    async def _vergleich(self, auftrag: Auftrag, a: Path, b: Path, zweite: str) -> Ergebnis:
        e = self._einstellungen()
        await self._holen(auftrag, zweite, b, e)
        if await asyncio.to_thread(_pruefsumme, a) == await asyncio.to_thread(_pruefsumme, b):
            return Ergebnis(gleich=True, verwertbar=True, hinweis="Beide Fassungen sind Byte für Byte gleich")

        def melden(von: float, bis: float, schritt: str) -> klang.Fortschritt:
            def f(anteil: float) -> None:
                auftrag.schritt, auftrag.fortschritt = schritt, round(von + (bis - von) * anteil, 3)
            return f

        melden(0.2, 0.2, "liest den Klang")(0)
        fa = await asyncio.to_thread(lambda: klang.merkmale(klang.dekodieren(a)))
        fb = await asyncio.to_thread(lambda: klang.merkmale(klang.dekodieren(b)))
        k = e.klang
        bild_a = await asyncio.to_thread(klang.vergleichen, fa, fb, gleich=k.gleich, mindestens_s=k.stelle_mindestens_s,
                                         fortschritt=melden(0.25, 0.6, "sucht A in B"))
        bild_b = await asyncio.to_thread(klang.vergleichen, fb, fa, gleich=k.gleich, mindestens_s=k.stelle_mindestens_s,
                                         fortschritt=melden(0.6, 0.95, "sucht B in A"))
        ergebnis = Ergebnis(gleich=False, verwertbar=bild_a.anteil_gemeinsam >= k.gemeinsam_mindestens,
                            dauer_a=bild_a.dauer, dauer_b=bild_b.dauer, anteil_gemeinsam=round(bild_a.anteil_gemeinsam, 3))
        if not ergebnis.verwertbar:
            ergebnis.hinweis = (f"Nur {bild_a.anteil_gemeinsam:.0%} von A kommen auch in B vor: "
                                "wohl nicht dieselbe Folge, keine Stellen genannt")
            return ergebnis
        auftrag.schritt = "hebt Signaturen auf"
        ergebnis.nur_in_a = await self._fundstellen(auftrag, a, bild_a.nur_hier, "a", e)
        ergebnis.nur_in_b = await self._fundstellen(auftrag, b, bild_b.nur_hier, "b", e)
        return ergebnis

    async def _fundstellen(self, auftrag: Auftrag, ton: Path, stellen: list[klang.Stelle], fassung: str,
                           e: Einstellungen) -> list[Fundstelle]:
        s = e.signaturen
        funde = []
        for stelle in stellen:
            kennung = None
            if s.aufheben and s.dauer_mindestens_s <= stelle.dauer <= s.dauer_hoechstens_s:
                try:
                    kennung = (await asyncio.to_thread(
                        self.signaturen.aufnehmen, ton, stelle.von, stelle.bis, quelle=auftrag.quelle, app=auftrag.app,
                        herkunft="vergleich", fassung=fassung, auftrag=auftrag.id)).id
                except SignaturFehler:
                    kennung = None  # die Stelle gilt trotzdem; nur der Ausschnitt fehlt
            funde.append(Fundstelle(von=stelle.von, bis=stelle.bis, signatur=kennung))
        return funde

    async def _holen(self, auftrag: Auftrag, adresse: str, ziel: Path, e: Einstellungen) -> None:
        auftrag.schritt, auftrag.fortschritt = "holt die zweite Fassung", 0.0
        grenze = e.grenzen.groesse_mb * 1024 * 1024
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(e.abruf.zeitgrenze_s, connect=20),
                                         headers={"User-Agent": e.abruf.kennung}, transport=self._transport) as c:
                async with c.stream("GET", adresse) as antwort:
                    if antwort.status_code != 200:
                        raise AbrufFehler(f"Anbieter antwortet mit {antwort.status_code}")
                    gesamt = int(antwort.headers.get("content-length") or 0)
                    geholt = 0
                    with ziel.open("wb") as f:
                        async for teil in antwort.aiter_bytes(1 << 16):
                            geholt += len(teil)
                            if geholt > grenze:
                                raise AbrufFehler(f"Zweite Fassung größer als {e.grenzen.groesse_mb} MB")
                            f.write(teil)
                            if gesamt:
                                auftrag.fortschritt = round(0.2 * min(1.0, geholt / gesamt), 3)
        except httpx.HTTPError as f:
            raise AbrufFehler(f"Zweite Fassung nicht abrufbar: {type(f).__name__}") from f
        if geholt == 0:
            raise AbrufFehler("Zweite Fassung ist leer")
