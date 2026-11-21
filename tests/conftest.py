"""Gemeinsame Aufbauten: künstlicher Ton (Inhalt mit eingesetzten Stellen), nachgestellte Kellerwolke und ein
nachgestellter Anbieter, der Fassung B ausliefert, dazu der Filter als ASGI-App."""

from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import numpy as np
import pytest

from propagandafilter.main import app_bauen

RATE = 16000
SCHLUESSEL = {
    "kw_app_radiohub": {"app": "RadioHub", "konto": "alpha", "konto_id": "00000000-0000-4000-8000-00000000000a"},
    "kw_app_fundus": {"app": "Fundus", "konto": "alpha", "konto_id": "00000000-0000-4000-8000-00000000000a"},
}
RADIOHUB = {"Authorization": "Bearer kw_app_radiohub"}
FUNDUS = {"Authorization": "Bearer kw_app_fundus"}
FOLGE = "https://anbieter.test/folge.mp3"


def klang(sekunden: float, saat: int) -> np.ndarray:
    """Etwas, das sich wie Inhalt verhält: alle 100 ms ein anderer Klang aus zwei Tönen und Rauschen."""
    zufall = np.random.default_rng(saat)
    teile = []
    for _ in range(int(sekunden * 10)):
        t = np.arange(RATE // 10) / RATE
        f1, f2 = zufall.uniform(200, 3000, 2)
        teile.append(zufall.uniform(0.1, 0.5) * np.sin(2 * np.pi * f1 * t) + zufall.uniform(0, 0.3) * np.sin(2 * np.pi * f2 * t)
                     + zufall.uniform(0.005, 0.05) * zufall.standard_normal(len(t)))
    return np.concatenate(teile)


def schreiben(pfad: Path, ton: np.ndarray) -> Path:
    with wave.open(str(pfad), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(ton, -1, 1) * 32000).astype("<i2").tobytes())
    return pfad


def s(sekunden: float) -> int:
    return int(sekunden * RATE)


@pytest.fixture(scope="session")
def fassungen(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """inhalt: 60 s; a: mit Spot (8 s) nach 20 s; b: ohne; anders: eine ganz andere Folge."""
    o = tmp_path_factory.mktemp("ton")
    inhalt, spot = klang(60, 1), klang(8, 2)
    return {
        "a": schreiben(o / "a.wav", np.concatenate([inhalt[:s(20)], spot, inhalt[s(20):]])),
        "b": schreiben(o / "b.wav", inhalt),
        "anders": schreiben(o / "anders.wav", klang(60, 3)),
    }


def kellerwolke() -> httpx.MockTransport:
    def antwort(r: httpx.Request) -> httpx.Response:
        kopf = r.headers.get("authorization", "")
        ich = SCHLUESSEL.get(kopf.removeprefix("Bearer "))
        return httpx.Response(200, json=ich) if ich else httpx.Response(401)
    return httpx.MockTransport(antwort)


class Anbieter:
    """Liefert unter FOLGE die eingestellte Datei aus (oder einen Fehler)."""

    def __init__(self) -> None:
        self.datei: Path | None = None
        self.status = 200
        self.abrufe = 0

    def transport(self) -> httpx.MockTransport:
        def antwort(r: httpx.Request) -> httpx.Response:
            self.abrufe += 1
            if str(r.url) != FOLGE or self.datei is None or self.status != 200:
                return httpx.Response(self.status if self.status != 200 else 404)
            return httpx.Response(200, content=self.datei.read_bytes(), headers={"content-type": "audio/wav"})
        return httpx.MockTransport(antwort)


@pytest.fixture
def anbieter() -> Anbieter:
    return Anbieter()


@pytest.fixture
async def filter_(tmp_path: Path, anbieter: Anbieter) -> AsyncIterator[httpx.AsyncClient]:
    app = app_bauen(tmp_path / "daten", kellerwolke=kellerwolke(), anbieter=anbieter.transport())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://filter") as c:
        yield c


async def warten(c: httpx.AsyncClient, kennung: str, bedingung: Callable[[dict], bool] = lambda a: a["zustand"] in ("fertig", "fehler"),
                 sekunden: float = 120) -> dict:
    for _ in range(int(sekunden * 10)):
        a = (await c.get(f"/api/v1/vergleiche/{kennung}", headers=RADIOHUB)).json()
        if bedingung(a):
            return a
        await asyncio.sleep(0.1)
    raise AssertionError(f"Auftrag {kennung} wurde nicht fertig")
