"""Bekannte Werbung im laufenden Strom: anlegen, wiedererkennen, Wirkung je Quelle, Urteil, Abbruch."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import FUNDUS, RADIOHUB, RATE, klang, s, schreiben
from propagandafilter.fingerabdruck import Verzeichnis, abdruecke

STUECK = 16000          # Bytes je Stück, bei 128 kbit/s eine Sekunde


def kodieren(ton: np.ndarray, art: str = "mp3", rate: str = "128k") -> bytes:
    roh = (np.clip(ton, -1, 1) * 32000).astype("<i2").tobytes()
    codec, form = ("libmp3lame", "mp3") if art == "mp3" else ("aac", "adts")
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "-", "-ar", "44100",
                           "-c:a", codec, "-b:a", rate, "-f", form, "-"], input=roh, capture_output=True, check=True).stdout


@pytest.fixture(scope="module")
def spot() -> np.ndarray:
    return klang(15, 42)


async def anlegen(c, ordner: Path, spot: np.ndarray, quelle: str = "sender-1", **mehr):
    datei = schreiben(ordner / "block.wav", np.concatenate([klang(3, 7), spot, klang(3, 8)]))
    return await c.post("/api/v1/signaturen", params={"quelle": quelle, "name": "Sender 1", "von": 3, "bis": 18, "herkunft": "angabe",
                                                      "werbender": "firma.de", **mehr},
                        content=datei.read_bytes(), headers=RADIOHUB)


async def strom(c, daten: bytes, quelle: str = "sender-1", art: str = "mp3", ab: float = 100.0) -> dict:
    s_ = (await c.post("/api/v1/stroeme", json={"quelle": quelle, "name": quelle, "art": art}, headers=RADIOHUB)).json()
    for i in range(0, len(daten), STUECK):
        r = await c.post(f"/api/v1/stroeme/{s_['id']}/ton", params={"ab": ab}, content=daten[i:i + STUECK], headers=RADIOHUB)
        assert r.status_code == 200, r.text
        await asyncio.sleep(0.01)
    vorher = -1.0
    for _ in range(100):                                       # bis der Ton zu Ende geprüft ist
        stand = (await c.get(f"/api/v1/stroeme/{s_['id']}", headers=RADIOHUB)).json()
        if stand["geprueft_bis"] == vorher and stand["geprueft_bis"] > ab + 5:
            return stand
        vorher = stand["geprueft_bis"]
        await asyncio.sleep(0.25)
    return stand


def test_fremder_ton_trifft_nicht(spot):
    v = Verzeichnis.aus([("a", *abdruecke(spot[::2].astype(np.float32)), 15.0)])
    h, t = abdruecke(klang(20, 99)[::2].astype(np.float32))
    assert not v.suchen(h, t, mindestens=30, anteil=0.04)


@pytest.mark.parametrize("art", ["mp3", "aac"])
async def test_bekannter_spot_wird_im_strom_erkannt(filter_, tmp_path, spot, art):
    r = await anlegen(filter_, tmp_path, spot)
    assert r.status_code == 201, r.text
    sig = r.json()
    assert sig["werbender"] == "firma.de" and [(q["quelle"], q["stand"]) for q in sig["quellen"]] == [("sender-1", "wirkt")]
    ton = np.concatenate([klang(12, 21), 0.5 * spot, klang(14, 22)])
    stand = await strom(filter_, kodieren(ton, art, "64k"), art=art)
    (stelle,) = stand["stellen"]
    assert stelle["signatur"] == sig["id"] and stelle["wirkt"] and not stelle["abgebrochen"]
    assert abs(stelle["von"] - 112.0) < 0.35 and abs(stelle["bis"] - 127.0) < 0.35
    assert stelle["sicher_ab"] < 118, "früh genug, um den Rest stummzuschalten"


async def test_woanders_nur_vorschlag_bis_man_ihn_annimmt(filter_, tmp_path, spot):
    sig = (await anlegen(filter_, tmp_path, spot)).json()
    ton = kodieren(np.concatenate([klang(8, 31), spot, klang(8, 32)]))
    assert [x["wirkt"] for x in (await strom(filter_, ton, "sender-2"))["stellen"]] == [False]
    r = await filter_.put(f"/api/v1/signaturen/{sig['id']}/quellen/sender-2", json={"stand": "wirkt"}, headers=RADIOHUB)
    assert {q["quelle"]: q["stand"] for q in r.json()["quellen"]} == {"sender-1": "wirkt", "sender-2": "wirkt"}
    assert [x["wirkt"] for x in (await strom(filter_, ton, "sender-2"))["stellen"]] == [True]


async def test_verworfenes_kommt_nicht_wieder(filter_, tmp_path, spot):
    sig = (await anlegen(filter_, tmp_path, spot)).json()
    await filter_.put(f"/api/v1/signaturen/{sig['id']}", json={"urteil": "verworfen"}, headers=RADIOHUB)
    ton = kodieren(np.concatenate([klang(8, 41), spot, klang(8, 42)]))
    assert (await strom(filter_, ton))["stellen"] == []
    r = await anlegen(filter_, tmp_path, spot)                  # noch einmal gemeldet: dieselbe, weiter verworfen
    assert r.status_code == 200 and (r.json()["id"], r.json()["urteil"]) == (sig["id"], "verworfen")


async def test_gleicher_klang_von_einem_zweiten_sender_ist_dieselbe(filter_, tmp_path, spot):
    eins = (await anlegen(filter_, tmp_path, spot)).json()
    r = await anlegen(filter_, tmp_path, 0.6 * spot, "sender-9")
    assert r.status_code == 200 and r.json()["id"] == eins["id"]
    assert {q["quelle"]: q["stand"] for q in r.json()["quellen"]} == {"sender-1": "wirkt", "sender-9": "wirkt"}


async def test_abgebrochener_spot_endet_wo_er_aufhoert(filter_, tmp_path, spot):
    await anlegen(filter_, tmp_path, spot)
    ton = kodieren(np.concatenate([klang(8, 51), spot[:s(7)], klang(16, 52)]))
    (stelle,) = (await strom(filter_, ton))["stellen"]
    assert stelle["abgebrochen"] and 112 < stelle["bis"] < 116.5


async def test_gemeinsames_ende_ist_kein_spot(filter_, tmp_path, spot):
    """Der Jingle des Senders am Ende eines Spots läuft auch allein; er darf nie den ganzen Spot behaupten (Oldie Antenne)."""
    await anlegen(filter_, tmp_path, spot)
    ton = kodieren(np.concatenate([klang(25, 61), spot[s(9):], klang(14, 62)]))
    assert (await strom(filter_, ton))["stellen"] == []


async def test_mitten_im_spot_verbunden(filter_, tmp_path, spot):
    await anlegen(filter_, tmp_path, spot)
    (stelle,) = (await strom(filter_, kodieren(np.concatenate([spot[s(7):], klang(14, 71)]))))["stellen"]
    assert stelle["von"] == 100.0 and abs(stelle["bis"] - 108.0) < 0.35


async def test_grenzen_und_rechte(filter_, tmp_path, spot):
    assert (await anlegen(filter_, tmp_path, spot, bis=4)).status_code == 422
    assert (await filter_.post("/api/v1/stroeme", json={"quelle": "x", "art": "ogg"}, headers=RADIOHUB)).status_code == 422
    s_ = (await filter_.post("/api/v1/stroeme", json={"quelle": "x", "art": "mp3"}, headers=RADIOHUB)).json()
    assert (await filter_.get(f"/api/v1/stroeme/{s_['id']}", headers=FUNDUS)).status_code == 403
    assert (await filter_.delete(f"/api/v1/stroeme/{s_['id']}", headers=RADIOHUB)).status_code == 204
    assert (await filter_.get(f"/api/v1/stroeme/{s_['id']}", headers=RADIOHUB)).status_code == 404
