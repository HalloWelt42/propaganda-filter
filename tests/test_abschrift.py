"""Mithören: Abschrift und Einordnung über die Leitstelle, ohne Schlüssel ruht es."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from appkern.leitstelle import LeitstellenFehler

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


@pytest.mark.parametrize("text,art,modell,erwartet,urteil", [
    ("Jetzt neu bei Musterfirma, nur solange der Vorrat reicht", "werbung", "m", ("werbung", "Musterfirma"), "bestaetigt"),
    ("Sie hören den besten Mix für das ganze Land", "sender", "m", ("sender", ""), "offen"),
    ("Jetzt neu bei Musterfirma, nur solange der Vorrat reicht", "werbung", "", ("unklar", ""), "offen"),
    ("la", "werbung", "m", ("ohne_sprache", ""), "offen"),
])
async def test_einordnung(tmp_path, text, art, modell, erwartet, urteil):
    b, kennung = buch(tmp_path)
    anfragen: list[httpx.Request] = []
    e = Einstellungen(leitstelle=Leitstelle(schluessel="probe", modell=modell))
    a = Abschrift(b, lambda: e, transport=leitstelle(text, art, "Musterfirma", anfragen))
    assert a.eingerichtet and a.faellig() == [], "ein einzelnes Stück wartet auf das Bündel"
    a.anstossen()
    await a.rechnen(a.faellig())
    assert a.faellig() == [], "jedes Stück nur einmal"
    s = b.eine(kennung)
    assert (s.einschaetzung, s.werbender) == erwartet and s.text == text
    assert (s.urteil, s.beurteilt_von) == (urteil, "selbst" if urteil == "bestaetigt" else ""), "nur ein sicherer Fund gibt sich selbst frei"
    assert all(r.headers["x-leitstelle-prioritaet"] == "hintergrund" and r.headers["x-leitstelle-art"] == "masse" for r in anfragen)
    assert len({r.headers["x-leitstelle-arbeit"] for r in anfragen}) == 1


async def test_angabe_des_senders_bleibt(tmp_path):
    b, kennung = buch(tmp_path, werbender="firma.de")
    e = Einstellungen(leitstelle=Leitstelle(schluessel="probe", modell="m"))
    a = Abschrift(b, lambda: e, transport=leitstelle("Werbung von jemand anderem hier", "werbung", "Anders", []))
    await a.rechnen([b.eine(kennung)])
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


async def test_buendel_erst_hoeren_dann_einordnen(tmp_path):
    """HELF-265: das Sprachmodell lädt je Bündel einmal, also kommt alles Hören vor allem Einordnen."""
    b, _ = buch(tmp_path)
    for _ in range(3):
        b.aufnehmen(tmp_path / "t.wav", 1, 11, quelle="S", app="", herkunft="lernlauf", fassung="", auftrag="")
    anfragen: list[httpx.Request] = []
    e = Einstellungen(leitstelle=Leitstelle(schluessel="probe", modell="m", buendel=4))
    a = Abschrift(b, lambda: e, transport=leitstelle("Jetzt neu bei Musterfirma", "werbung", "Musterfirma", anfragen))
    assert len(a.faellig()) == 4, "das Bündel ist voll"
    await a.rechnen(a.faellig())
    assert ["stt" if r.url.path.endswith("/stt") else "chat" for r in anfragen] == ["stt"] * 4 + ["chat"] * 4


async def test_abbruch_wird_fortgesetzt_ohne_neu_zu_hoeren(tmp_path):
    b, kennung = buch(tmp_path)
    anfragen: list[httpx.Request] = []
    gut = leitstelle("Jetzt neu bei Musterfirma", "werbung", "Musterfirma", anfragen)
    e = Einstellungen(leitstelle=Leitstelle(schluessel="probe", modell="m"))

    def halb(r: httpx.Request) -> httpx.Response:
        return gut.handler(r) if r.url.path.endswith("/stt") else httpx.Response(503, json={"error": {"code": "halt", "message": "Halt"}})
    with pytest.raises(LeitstellenFehler):
        await Abschrift(b, lambda: e, transport=httpx.MockTransport(halb)).rechnen([b.eine(kennung)])
    assert b.eine(kennung).text and b.eine(kennung).einschaetzung == ""
    anfragen.clear()
    await Abschrift(b, lambda: e, transport=gut).rechnen(b.ohne_einordnung(9))
    assert [r.url.path.endswith("/stt") for r in anfragen] == [False] and b.eine(kennung).werbender == "Musterfirma"


def test_wartet_nie_zu_lange(tmp_path):
    b, _ = buch(tmp_path)
    a = Abschrift(b, lambda: Einstellungen(leitstelle=Leitstelle(schluessel="probe", spaetestens_min=120)))
    jetzt = datetime.now(UTC)
    assert a.faellig(jetzt + timedelta(minutes=100)) == [] and len(a.faellig(jetzt + timedelta(minutes=121))) == 1
