"""Ein Vergleich als Ablauf: je Kennung eine weitere Fassung holen, gleich in Bytes?, sonst am Klang ausrichten (beide
Richtungen), verwertbar?, die Stellen aller Vergleiche vereinigen und als Signaturen aufheben.

Fassung A reicht die App ein (ihre gespeicherte Datei, nur für die gelten die Zeiten); die weiteren Fassungen holt der
Filter selbst von derselben Adresse beim Anbieter, so wie weitere Abrufe sie bekommen. Die Arbeitsdateien liegen je Auftrag in
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
from .modelle import Abgleich, Auftrag, Ergebnis, Fundstelle
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
            return await self._vergleich(auftrag, ordner, zweite)
        finally:
            self.wegraeumen(auftrag.id)

    async def _vergleich(self, auftrag: Auftrag, ordner: Path, adresse: str) -> Ergebnis:
        """Je Kennung eine weitere Fassung holen und mit A vergleichen; die Werbeplätze in A sind alles, was in
        irgendeiner Fassung fehlt (ein Spot, der in einer Fassung zufällig gleich ist, fällt erst in einer anderen auf)."""
        e = self._einstellungen()
        k = e.klang
        a = ordner / "a"
        summe_a = await asyncio.to_thread(_pruefsumme, a)
        anzahl = len(e.abruf.kennungen)
        fa = None
        abgleiche: list[Abgleich] = []
        stellen_a: list[list[klang.Stelle]] = []
        for i, kennung in enumerate(e.abruf.kennungen):
            anfang, ende = i / anzahl, (i + 1) / anzahl

            def teil(von: float, bis: float, anfang: float = anfang, ende: float = ende) -> tuple[float, float]:
                """Anteil am ganzen Auftrag für einen Abschnitt der Arbeit an dieser Fassung"""
                return anfang + (ende - anfang) * von, anfang + (ende - anfang) * bis
            nr = f"Fassung {i + 2} von {anzahl + 1}"
            b = ordner / f"b{i + 1}"
            await self._holen(auftrag, adresse, b, e, kennung, f"holt {nr}", teil(0, 0.15))
            if await asyncio.to_thread(_pruefsumme, b) == summe_a:
                abgleiche.append(Abgleich(kennung=kennung, gleich=True))
                b.unlink(missing_ok=True)
                continue
            self._melden(auftrag, f"liest den Klang ({nr})", teil(0.15, 0.15)[0])
            if fa is None:
                fa = await asyncio.to_thread(lambda: klang.merkmale(klang.dekodieren(a)))
            fb = await asyncio.to_thread(lambda: klang.merkmale(klang.dekodieren(b)))
            bild_a = await asyncio.to_thread(klang.vergleichen, fa, fb, gleich=k.gleich, mindestens_s=k.stelle_mindestens_s,
                                             fortschritt=self._fortschritt(auftrag, f"sucht A in {nr}", *teil(0.2, 0.6)))
            abgleich = Abgleich(kennung=kennung, gleich=False, verwertbar=bild_a.anteil_gemeinsam >= k.gemeinsam_mindestens,
                                dauer=bild_a.dauer, anteil_gemeinsam=round(bild_a.anteil_gemeinsam, 3))
            if abgleich.verwertbar:
                bild_b = await asyncio.to_thread(klang.vergleichen, fb, fa, gleich=k.gleich, mindestens_s=k.stelle_mindestens_s,
                                                 fortschritt=self._fortschritt(auftrag, f"sucht {nr} in A", *teil(0.6, 1.0)))
                abgleich.dauer = bild_b.dauer
                abgleich.nur_in_a = [Fundstelle(von=x.von, bis=x.bis) for x in bild_a.nur_hier]
                abgleich.nur_in_b = await self._fundstellen(auftrag, b, bild_b.nur_hier, f"b{i + 1}", e)
                stellen_a.append(bild_a.nur_hier)
            abgleiche.append(abgleich)
            b.unlink(missing_ok=True)
        verschieden = [x for x in abgleiche if not x.gleich]
        ergebnis = Ergebnis(gleich=not verschieden, verwertbar=any(x.verwertbar for x in abgleiche), abgleiche=abgleiche)
        if ergebnis.gleich:
            ergebnis.hinweis = "Alle Fassungen sind Byte für Byte gleich"
            return ergebnis
        verwertbar = [x for x in verschieden if x.verwertbar]
        if not verwertbar:
            ergebnis.hinweis = "Keine weitere Fassung ist dieselbe Folge; keine Stellen genannt"
            return ergebnis
        ergebnis.dauer_a = round(len(fa) * klang.ZEILE_S, 2)
        ergebnis.anteil_gemeinsam = min(x.anteil_gemeinsam or 0 for x in verwertbar)
        self._melden(auftrag, "hebt Signaturen auf", 0.99)
        ergebnis.nur_in_a = await self._fundstellen(auftrag, a, klang.vereinigen(stellen_a, k.luecke_hoechstens_s), "a", e)
        return ergebnis

    @staticmethod
    def _melden(auftrag: Auftrag, schritt: str, anteil: float) -> None:
        auftrag.schritt, auftrag.fortschritt = schritt, round(min(1.0, anteil), 3)

    @classmethod
    def _fortschritt(cls, auftrag: Auftrag, schritt: str, von: float, bis: float) -> klang.Fortschritt:
        return lambda anteil: cls._melden(auftrag, schritt, von + (bis - von) * anteil)

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

    async def _holen(self, auftrag: Auftrag, adresse: str, ziel: Path, e: Einstellungen, kennung: str, schritt: str,
                     anteil: tuple[float, float]) -> None:
        self._melden(auftrag, schritt, anteil[0])
        grenze = e.grenzen.groesse_mb * 1024 * 1024
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(e.abruf.zeitgrenze_s, connect=20),
                                         headers={"User-Agent": kennung}, transport=self._transport) as c:
                async with c.stream("GET", adresse) as antwort:
                    if antwort.status_code != 200:
                        raise AbrufFehler(f"Anbieter antwortet mit {antwort.status_code}")
                    gesamt = int(antwort.headers.get("content-length") or 0)
                    geholt = 0
                    with ziel.open("wb") as f:
                        async for teil in antwort.aiter_bytes(1 << 16):
                            geholt += len(teil)
                            if geholt > grenze:
                                raise AbrufFehler(f"Weitere Fassung größer als {e.grenzen.groesse_mb} MB")
                            f.write(teil)
                            if gesamt:
                                self._melden(auftrag, schritt, anteil[0] + (anteil[1] - anteil[0]) * min(1.0, geholt / gesamt))
        except httpx.HTTPError as f:
            raise AbrufFehler(f"Weitere Fassung nicht abrufbar: {type(f).__name__}") from f
        if geholt == 0:
            raise AbrufFehler("Weitere Fassung ist leer")
