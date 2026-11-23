"""Lernlauf: Wiederholungen finden, Sendedaten trennen, Auswahl der Sender, ein ganzer Lauf über die Schnittstelle."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import numpy as np
import pytest

from conftest import FUNDUS, RADIOHUB, kellerwolke, klang, s
from propagandafilter.lernbuch import Lernbuch, Lernsender
from propagandafilter.lernlauf import abdruecke_lang, im_fenster, ist_musik
from propagandafilter.main import app_bauen
from propagandafilter.mitschnitt import Sendedaten, Titelzeile
from propagandafilter.wiederholung import Archiv, Aufnahme, Kandidat, ausduennen, suchen
from test_strom import kodieren

SPOT, LIED = klang(20, 42), klang(150, 43)


def aufnahme(name: str, *teile: np.ndarray) -> Aufnahme:
    x = np.concatenate(teile)[::2].astype(np.float32)
    return Aufnahme(name, name, len(x) / 8000, *ausduennen(*abdruecke_lang(x)))


def test_spot_auf_zwei_sendern_ist_kandidat_lied_und_rand_nicht():
    a = aufnahme("a", klang(40, 1), SPOT, klang(30, 2), LIED, klang(20, 3), klang(12, 9))
    b = aufnahme("b", klang(25, 4), LIED, klang(30, 5), 0.5 * SPOT, klang(50, 6))
    c = aufnahme("c", klang(12, 9), klang(120, 7))                       # gemeinsames Stück am Rand
    (k,) = suchen(a, Archiv([a, b, c]))
    assert abs(k.von - 40) < 0.6 and abs(k.bis - 60) < 0.6 and [v.quelle for v in k.vorkommen] == ["b"]
    assert suchen(c, Archiv([a, b, c])) == []


def test_wiederholung_in_sich_selbst_zaehlt_erst_mit_abstand():
    nah = aufnahme("n", klang(20, 1), SPOT, klang(60, 2), SPOT, klang(20, 3))
    assert suchen(nah, Archiv([nah])) == []
    weit = aufnahme("w", klang(20, 1), SPOT, klang(640, 2), SPOT, klang(20, 3))
    assert {round(k.von) for k in suchen(weit, Archiv([weit]))} == {20, 680}


def test_schleifen_sind_musik():
    takt = klang(9, 51)
    a = aufnahme("a", klang(30, 1), takt, takt, takt, klang(30, 2))
    b = aufnahme("b", klang(50, 3), takt, takt, klang(30, 4))
    assert suchen(a, Archiv([a, b])) == []


def test_kandidat_im_langen_titel_ist_musik():
    titel = [Titelzeile(0, "Werbung"), Titelzeile(30, "Band - Lied"), Titelzeile(220, "")]
    assert ist_musik(Kandidat(100, 120, ()), titel, 300)
    assert not ist_musik(Kandidat(5, 25, ()), titel, 300) and not ist_musik(Kandidat(225, 245, ()), titel, 300)


@pytest.mark.parametrize("stueck", [1, 7, 4096])
def test_sendedaten_werden_getrennt(stueck):
    ton = bytes(range(256)) * 4
    meta = "StreamTitle='Ä - Lied';StreamUrl='';".encode()
    meta += b"\0" * (-len(meta) % 16)
    roh = ton[:300] + bytes([len(meta) // 16]) + meta + ton[300:600] + b"\0" + ton[600:900]
    d, aus, titel = Sendedaten(300), b"", []
    for i in range(0, len(roh), stueck):
        t, n = d.nehmen(roh[i:i + stueck])
        aus, titel = aus + t, titel + n
    assert aus == ton[:900] and titel == ["Ä - Lied"]


def test_zeitfenster():
    assert im_fenster("01:00", "01:00", "06:00") and not im_fenster("06:00", "01:00", "06:00")
    assert im_fenster("23:30", "23:00", "05:00") and im_fenster("04:00", "23:00", "05:00") and not im_fenster("12:00", "23:00", "05:00")


def test_auswahl_der_sender(tmp_path):
    b = Lernbuch(tmp_path)
    L = lambda q, grund, gewicht=1.0: Lernsender(quelle=q, name=q, adresse="http://x.test/" + q, grund=grund, gewicht=gewicht)
    b.sender_setzen([L("bekannt", "bekannt"), L("fav", "favorit"), L("oft", "gehoert", 12), L("aehnlich", "aehnlich")])
    assert [x.quelle for x in b.naechste(9)] == ["oft", "fav", "aehnlich", "bekannt"]
    lauf = b.lauf_beginnen()
    b.gehoert(lauf, "oft", 1800, 2, 1)                   # eben gehört: erst in 18 Stunden wieder
    b.gescheitert(lauf, "fav", "nicht erreichbar")
    b.ausnehmen("bekannt", True)
    assert [x.quelle for x in b.naechste(9)] == ["aehnlich"]
    b.sender_setzen([L("neu", "favorit")])               # die anderen ruhen mit ihren Zahlen
    assert [x.quelle for x in b.naechste(9)] == ["neu"]
    assert {x.quelle: (x.dabei, x.kandidaten, x.stunden) for x in b.sender()}["oft"] == (False, 2, 0.5)
    assert (b.laeufe()[0].sender, b.laeufe()[0].fehler, b.laeufe()[0].neu) == (1, 1, 2)


async def test_ganzer_lauf(tmp_path: Path, monkeypatch):
    toene = {"http://a.test/": kodieren(np.concatenate([klang(60, 1), SPOT, klang(60, 2)])),
             "http://b.test/": kodieren(np.concatenate([klang(45, 3), SPOT, klang(75, 4)]))}

    def sender(r: httpx.Request) -> httpx.Response:
        ton = toene.get(str(r.url))
        return httpx.Response(200, content=ton, headers={"content-type": "audio/mpeg"}) if ton else httpx.Response(404)

    app = app_bauen(tmp_path / "daten", kellerwolke=kellerwolke(), anbieter=httpx.MockTransport(sender))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://filter", headers=RADIOHUB) as c:
        assert (await c.get("/api/v1/lernlauf", headers=FUNDUS)).status_code == 403
        stand = (await c.put("/api/v1/lernlauf", json={"an": False, "von": "02:00"})).json()
        assert (stand["an"], stand["von"], stand["bis"]) == (False, "02:00", "06:00")
        liste = [{"quelle": q, "name": q.upper(), "adresse": f"http://{q}.test/", "grund": "favorit"} for q in ("a", "b", "weg")]
        assert (await c.put("/api/v1/lernlauf/sender", json=liste)).json()["sender"] == 3
        assert [x["name"] for x in (await c.post("/api/v1/lernlauf/jetzt", json={"minuten": 30})).json()["laufend"]] == ["A", "B", "WEG"]
        for _ in range(300):
            stand = (await c.get("/api/v1/lernlauf")).json()
            if not stand["laufend"]:
                break
            await asyncio.sleep(0.1)
        (sig,) = (await c.get("/api/v1/signaturen")).json()["signaturen"]
        assert (sig["herkunft"], sig["urteil"]) == ("lernlauf", "offen") and abs(sig["dauer_s"] - 20) < 1.5
        assert {q["quelle"]: q["stand"] for q in sig["quellen"]} == {"a": "vorschlag", "b": "vorschlag"}
        st = {x["quelle"]: x for x in (await c.get("/api/v1/lernlauf/sender")).json()}
        assert "404" in st["weg"]["fehler"] and st["a"]["kandidaten"] + st["b"]["kandidaten"] == 1
        assert (await c.put("/api/v1/lernlauf/sender/a", json={"aus": True})).json()[0]["quelle"]
        assert list((tmp_path / "daten" / "lernlauf" / "arbeit").iterdir()) == [], "der Ton bleibt nicht"
        await c.put(f"/api/v1/signaturen/{sig['id']}", json={"urteil": "bestaetigt"})
        assert {q["stand"] for q in (await c.get("/api/v1/signaturen")).json()["signaturen"][0]["quellen"]} == {"wirkt"}
        await c.post("/api/v1/lernlauf/jetzt", json={"minuten": 0})
