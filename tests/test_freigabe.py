"""Regel der Freigabe (RAD-154): ein sicherer Fund wirkt ohne Durchsicht, und zwar bei jedem Sender; unsichere bleiben
Vorschlag, verworfene bleiben verworfen. Der Bestand lässt sich einmalig nachbewerten."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import klang, schreiben
from propagandafilter import nachbewerten
from propagandafilter.einstellungen import Einstellungen, Einstellungsquelle, Freigabe
from propagandafilter.signaturen import SCHEMA, STUFEN, Signaturbuch, sicherer_fund

REGEL = Freigabe()


def anlegen(b: Signaturbuch, ordner: Path, herkunft: str = "lernlauf", einschaetzung: str = "werbung", werbender: str = "Musterfirma") -> str:
    ton = ordner / "t.wav"
    if not ton.exists():
        schreiben(ton, klang(12, 5))
    kennung = b.aufnehmen(ton, 1, 11, quelle="Sender 1", app="", herkunft=herkunft, fassung="", auftrag="", quelle_kennung="sender-1").id
    b.text_setzen(kennung, "Jetzt neu bei Musterfirma", einschaetzung, werbender)
    return kennung


@pytest.fixture
def buch(tmp_path: Path) -> Iterator[Signaturbuch]:
    b = Signaturbuch(tmp_path / "sig")
    yield b
    b.schliessen()


def test_vorgaben():
    e = Einstellungen()
    assert e.freigabe.selbst_bei_sicherem_fund and e.freigabe.herkuenfte == ["lernlauf"] and e.wiedererkennen.ueberall


@pytest.mark.parametrize("herkunft,einschaetzung,werbender,regel,sicher", [
    ("lernlauf", "werbung", "Musterfirma", REGEL, True),
    ("lernlauf", "werbung", " ", REGEL, False),                       # Werbender nicht erkannt
    ("lernlauf", "unklar", "Musterfirma", REGEL, False),
    ("lernlauf", "sender", "", REGEL, False),
    ("vergleich", "werbung", "Musterfirma", REGEL, False),             # Herkunft nicht in der Regel
    ("vergleich", "werbung", "Musterfirma", Freigabe(herkuenfte=["lernlauf", "vergleich"]), True),
    ("lernlauf", "werbung", "Musterfirma", Freigabe(selbst_bei_sicherem_fund=False), False),
])
def test_regel(buch, tmp_path, herkunft, einschaetzung, werbender, regel, sicher):
    assert sicherer_fund(buch.eine(anlegen(buch, tmp_path, herkunft, einschaetzung, werbender)), regel) is sicher


def test_selbst_freigegeben_wirkt_ueberall_und_bleibt_verwerfbar(buch, tmp_path):
    kennung = anlegen(buch, tmp_path)
    assert not buch.wirkt(kennung, "sender-1", ueberall=True), "vor der Freigabe nur Vorschlag"
    assert buch.selbst_freigeben(kennung, REGEL)
    s = buch.eine(kennung)
    assert (s.urteil, s.beurteilt_von) == ("bestaetigt", "selbst") and s.beurteilt
    assert buch.wirkt(kennung, "sender-1", ueberall=True) and buch.wirkt(kennung, "anderer-sender", ueberall=True)
    assert not buch.wirkt(kennung, "anderer-sender", ueberall=False), "ohne ueberall nur dort, wo sie auffiel"
    assert buch.faecher() == {"pruefen": 0, "wirkt": 1, "verworfen": 0}
    buch.beurteilen(kennung, "verworfen")
    assert buch.eine(kennung).beurteilt_von == "mensch" and not buch.wirkt(kennung, "sender-1", ueberall=True)
    assert not buch.selbst_freigeben(kennung, REGEL) and buch.nachbewerten(REGEL, anwenden=True) == []
    assert buch.eine(kennung).urteil == "verworfen", "verworfen bleibt verworfen"


def test_nachbewerten_erst_vorschau_dann_anwenden(buch, tmp_path):
    sicher = anlegen(buch, tmp_path)
    unsicher = anlegen(buch, tmp_path, einschaetzung="unklar", werbender="")
    vorschau = buch.nachbewerten(REGEL, anwenden=False)
    assert [s.id for s in vorschau] == [sicher] and buch.eine(sicher).urteil == "offen", "die Vorschau ändert nichts"
    assert [s.id for s in buch.nachbewerten(REGEL, anwenden=True)] == [sicher]
    assert (buch.eine(sicher).urteil, buch.eine(unsicher).urteil) == ("bestaetigt", "offen")
    assert buch.nachbewerten(REGEL, anwenden=True) == [], "ein zweites Mal ist nichts mehr offen"


def test_befehl_nachbewerten(tmp_path, capsys):
    daten = tmp_path / "daten"
    assert nachbewerten.ausfuehren(daten, anwenden=False) == 2 and not daten.exists(), "legt keinen Datenordner an"
    daten.mkdir()
    Einstellungsquelle(daten / "einstellungen.toml")
    b = Signaturbuch(daten / "signaturen")
    kennung = anlegen(b, tmp_path)
    b.schliessen()
    assert nachbewerten.ausfuehren(daten, anwenden=False) == 0
    assert "1 Signaturen würden freigegeben" in capsys.readouterr().out
    assert nachbewerten.ausfuehren(daten, anwenden=True) == 0
    assert "1 Signaturen selbst freigegeben" in capsys.readouterr().out
    b = Signaturbuch(daten / "signaturen")
    assert b.eine(kennung).urteil == "bestaetigt"
    b.schliessen()


def test_stufe_merkt_wer_bisher_urteilte(tmp_path):
    """Bestand von vor der Regel: wer schon geurteilt hat, war ein Mensch."""
    ordner = tmp_path / "sig"
    ordner.mkdir()
    db = sqlite3.connect(ordner / "signaturen.db", isolation_level=None)
    db.executescript(SCHEMA + STUFEN[0] + "pragma user_version = 1;")
    for kennung, urteil in (("a", "bestaetigt"), ("b", "offen"), ("c", "verworfen")):
        db.execute("insert into signaturen (id, quelle, app, herkunft, fassung, von_s, bis_s, dauer_s, datei, auftrag, angelegt, urteil)"
                   " values (?, 'S', '', 'lernlauf', '', 0, 10, 10, 'x.m4a', '', '2026-11-20T00:00:00+00:00', ?)", (kennung, urteil))
    db.close()
    b = Signaturbuch(ordner)
    assert [b.eine(k).beurteilt_von for k in "abc"] == ["mensch", "", "mensch"]
    b.schliessen()
