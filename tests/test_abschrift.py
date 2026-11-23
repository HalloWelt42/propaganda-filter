"""Mithören: Abschrift und Einordnung über die Leitstelle, ohne Schlüssel ruht es."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest

from conftest import klang, schreiben
from propagandafilter.abschrift import Abschrift
from propagandafilter.einstellungen import Einstellungen, Leitstelle
from propagandafilter.signaturen import Signaturbuch


def leitstelle(text: str, art: str, werbender: str, anfragen: list[httpx.Request]) -> httpx.MockTransport:
    def antwort(r: httpx.Request) -> httpx.Response:
        anfragen.append(r)
        if r.url.path.endswith("/stt"):
            return httpx.Response(200, json={"segments": [{"start": 0, "end": 5, "text": text}], "language": "de"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"art": art, "werbender": werbender})}}]})
    return httpx.MockTransport(antwort)


def buch(ordner: Path, werbender: str = "") -> tuple[Signaturbuch, str]:
    b = Signaturbuch(ordner / "sig")
    ton = schreiben(ordner / "t.wav", klang(12, 5))
    return b, b.aufnehmen(ton, 1, 11, quelle="Sender", app="", herkunft="lernlauf", fassung="", auftrag="", werbender=werbender).id


@pytest.mark.parametrize("text,art,modell,erwartet", [
    ("Jetzt neu bei Musterfirma, nur solange der Vorrat reicht", "werbung", "m", ("werbung", "Musterfirma")),
    ("Sie hören den besten Mix für das ganze Land", "sender", "m", ("sender", "")),
    ("Jetzt neu bei Musterfirma, nur solange der Vorrat reicht", "werbung", "", ("unklar", "")),
    ("la", "werbung", "m", ("ohne_sprache", "")),
])
async def test_einordnung(tmp_path, text, art, modell, erwartet):
    b, kennung = buch(tmp_path)
    anfragen: list[httpx.Request] = []
    e = Einstellungen(leitstelle=Leitstelle(schluessel="probe", modell=modell))
    a = Abschrift(b, lambda: e, transport=leitstelle(text, art, "Musterfirma", anfragen))
    assert a.eingerichtet and await a.runde() == 1 and await a.runde() == 0, "jedes Stück nur einmal"
    s = b.eine(kennung)
    assert (s.einschaetzung, s.werbender) == erwartet and s.text == text and s.urteil == "offen"
    assert all(r.headers["x-leitstelle-prioritaet"] == "hintergrund" for r in anfragen)


async def test_angabe_des_senders_bleibt(tmp_path):
    b, kennung = buch(tmp_path, werbender="firma.de")
    e = Einstellungen(leitstelle=Leitstelle(schluessel="probe", modell="m"))
    await Abschrift(b, lambda: e, transport=leitstelle("Werbung von jemand anderem hier", "werbung", "Anders", [])).runde()
    assert b.eine(kennung).werbender == "firma.de"


def test_durchsicht_nach_aussicht(tmp_path):
    b, erste = buch(tmp_path)
    ton = tmp_path / "t.wav"
    ids = {"eigen": erste}
    for name in ("werbung", "neu", "oft"):
        ids[name] = b.aufnehmen(ton, 1, 11, quelle="S", app="", herkunft="lernlauf", fassung="", auftrag="", quelle_kennung="u1").id
    b.text_setzen(erste, "x", "sender", "")
    b.text_setzen(ids["werbung"], "x", "werbung", "Firma")
    b.text_setzen(ids["oft"], "x", "werbung", "Andere")
    b.treffer(ids["oft"], "u1", "S")
    assert [s.id for s in b.im_fach("pruefen", 0, 9)] == [ids["oft"], ids["werbung"], ids["neu"], ids["eigen"]]
    assert [s.id for s in b.im_fach("pruefen", 1, 2)] == [ids["werbung"], ids["neu"]]
    b.beurteilen(ids["oft"], "bestaetigt")
    assert b.treffer(ids["oft"], "u9", "Neuer Sender") == "vorschlag"          # bestätigt, aber an einer neuen Quelle: wieder zu prüfen
    b.beurteilen(erste, "verworfen")
    assert b.faecher() == {"pruefen": 3, "wirkt": 0, "verworfen": 1}


def test_ohne_schluessel_ruht_es(tmp_path):
    assert not Abschrift(Signaturbuch(tmp_path / "sig"), Einstellungen).eingerichtet
