"""Signaturen: bekannte Werbung als Ausschnitt mit Klang-Fingerabdruck. Sie kommen aus dem Vergleich zweier Fassungen, aus
angekündigten Spots (Sendedaten), aus dem Schnitt und aus dem Lernlauf.

Jede Signatur ist eine kurze Tondatei im Datenordner und eine Zeile in signaturen.db. Ein Mensch kann sie anhören und
bestätigen oder verwerfen (Irrtümer herausfiltern); verworfene bleiben mit ihrem Urteil stehen, damit derselbe Irrtum
nicht wieder als neu erscheint. Gelöscht wird hier nichts.
"""

from __future__ import annotations

import sqlite3
import subprocess
import uuid
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel

from .einstellungen import Wiedererkennen
from .fingerabdruck import RATE, Verzeichnis, abdruecke
from .klang import KlangFehler, dekodieren

Urteil = Literal["offen", "bestaetigt", "verworfen"]
Stand = Literal["wirkt", "vorschlag"]
Fach = Literal["pruefen", "wirkt", "verworfen"]

#: aus diesen Herkünften wirkt eine Signatur in ihrer eigenen Quelle, auch bevor ein Mensch sie bestätigt
SOFORT = frozenset({"vergleich", "angabe", "schnitt"})

SCHEMA = """
create table if not exists signaturen (
  id text primary key,
  quelle text not null,          -- was die App dazu sagt, etwa 'Podcast: Folgentitel'
  app text not null,             -- welche App den Vergleich bestellt hat
  herkunft text not null,        -- wie die Stelle gefunden wurde: 'vergleich'
  fassung text not null,         -- 'a' (eingereicht) oder 'b' (vom Filter geholt)
  von_s real not null,           -- Lage in jener Fassung
  bis_s real not null,
  dauer_s real not null,
  datei text not null,           -- relativ zum Ordner der Signaturen
  auftrag text not null,
  angelegt text not null,
  urteil text not null default 'offen',
  beurteilt text
);
create index if not exists signaturen_urteil on signaturen(urteil, angelegt);
"""

# Stufen nach der ersten Fassung, der Reihe nach; der Stand steht in user_version
STUFEN = [
    """
alter table signaturen add column werbender text not null default '';
alter table signaturen add column text text not null default '';       -- gesprochener Text, sobald transkribiert
alter table signaturen add column einschaetzung text not null default '';   -- wie der Text klingt: werbung, sender, anderes, ohne_sprache, unklar
alter table signaturen add column abdruck blob;                        -- Zeilen und Abdrücke (int32), gepackt
create table signatur_quellen (
  signatur text not null references signaturen(id),
  quelle text not null,                  -- Kennung der Quelle bei der App (Sender, Podcast)
  name text not null,
  stand text not null,                   -- 'wirkt' oder 'vorschlag'
  treffer integer not null default 0,
  zuerst text not null,
  zuletzt text not null,
  primary key (signatur, quelle)
);
create index signatur_quellen_quelle on signatur_quellen(quelle);
""",
]

#: in welchem Fach der Durchsicht eine Signatur liegt: verworfen; zu prüfen (Vorschlag des Lernlaufs oder bekannt, aber an einer
#: neuen Quelle gehört); sonst wirkt sie
FACH_SQL = """case when s.urteil = 'verworfen' then 'verworfen'
  when s.urteil = 'offen' and s.herkunft = 'lernlauf' then 'pruefen'
  when exists (select 1 from signatur_quellen q where q.signatur = s.id and q.stand = 'vorschlag') then 'pruefen'
  else 'wirkt' end"""
#: was am ehesten Werbung ist, zuerst
RANG_SQL = "case s.einschaetzung when 'werbung' then 0 when 'anderes' then 2 when 'sender' then 3 when 'ohne_sprache' then 4 else 1 end"
TREFFER_SQL = "coalesce((select sum(q.treffer) from signatur_quellen q where q.signatur = s.id), 0)"

ENDUNG = ".m4a"


class Quelle(BaseModel):
    """Wo eine Signatur vorkam und ob sie dort wirkt."""

    quelle: str
    name: str
    stand: Stand
    treffer: int
    zuerst: str
    zuletzt: str


class Signatur(BaseModel):
    id: str
    quelle: str
    app: str
    herkunft: str
    fassung: str
    von_s: float
    bis_s: float
    dauer_s: float
    auftrag: str
    angelegt: str
    urteil: Urteil
    beurteilt: str | None
    werbender: str = ""
    text: str = ""
    einschaetzung: str = ""
    quellen: list[Quelle] = []


class SignaturFehler(Exception):
    pass


def _packen(h: np.ndarray, t: np.ndarray) -> bytes:
    return zlib.compress(np.stack([h, t]).astype(np.int32).tobytes(), 6)


def _auspacken(daten: bytes) -> tuple[np.ndarray, np.ndarray]:
    beide = np.frombuffer(zlib.decompress(daten), np.int32).reshape(2, -1).astype(np.int64)
    return beide[0], beide[1]


def _jetzt() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Signaturbuch:
    def __init__(self, ordner: Path) -> None:
        self.ordner = ordner
        self.ton = ordner / "ton"
        self.ton.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(ordner / "signaturen.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)
        self._stufen()
        self._verzeichnis: Verzeichnis | None = None

    def _stufen(self) -> None:
        stand = self._db.execute("pragma user_version").fetchone()[0]
        for nummer, stufe in enumerate(STUFEN[stand:], stand + 1):
            self._db.executescript(f"begin;{stufe}pragma user_version = {nummer};commit;")

    def schliessen(self) -> None:
        self._db.close()

    def aufnehmen(self, ton: Path, von: float, bis: float, *, quelle: str, app: str, herkunft: str, fassung: str,
                  auftrag: str, werbender: str = "", quelle_kennung: str = "") -> Signatur:
        """Den Ausschnitt [von, bis) aus der Tondatei schneiden und als offene Signatur ablegen. `quelle_kennung`: in dieser
        Quelle kommt sie vor (Kennung bei der App)."""
        kennung = str(uuid.uuid4())
        datei = f"{kennung}{ENDUNG}"
        ziel = self.ton / datei
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{von:.2f}", "-to", f"{bis:.2f}", "-i", str(ton),
                            "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "96k", str(ziel)], capture_output=True)
        if r.returncode != 0 or not ziel.is_file():
            ziel.unlink(missing_ok=True)
            raise SignaturFehler(f"Ausschnitt nicht schneidbar: {r.stderr.decode(errors='replace').strip()[:300]}")
        self._db.execute(
            "insert into signaturen (id, quelle, app, herkunft, fassung, von_s, bis_s, dauer_s, datei, auftrag, angelegt,"
            " werbender, abdruck) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kennung, quelle, app, herkunft, fassung, von, bis, round(bis - von, 2), datei, auftrag, _jetzt(), werbender,
             _packen(*abdruecke(dekodieren(ziel)))))
        if quelle_kennung:
            self._db.execute("insert into signatur_quellen (signatur, quelle, name, stand, treffer, zuerst, zuletzt)"
                             " values (?, ?, ?, ?, 0, ?, ?)",
                             (kennung, quelle_kennung, quelle, "wirkt" if herkunft in SOFORT else "vorschlag", _jetzt(), _jetzt()))
        self._verzeichnis = None
        return self.eine(kennung)

    def gleiche(self, ton: Path, wo: tuple[float, float], e: Wiedererkennen) -> str | None:
        """Kennung einer vorhandenen Signatur mit demselben Klang und etwa derselben Länge (auch einer verworfenen: ein Irrtum
        soll nicht neu erscheinen), sonst None."""
        x = dekodieren(ton)[int(wo[0] * RATE):int(wo[1] * RATE)]
        dauer, n = len(x) / RATE, int(4 * RATE)
        mitte = max(0, len(x) // 2 - n // 2)
        h, t = abdruecke(x[mitte:mitte + n])
        v = self.verzeichnis(alle=True)
        for treffer in v.suchen(h, t, mindestens=e.punkte_sicher, anteil=e.anteil_sicher):
            if abs(v.laengen[v.spots.index(treffer.spot)] - dauer) <= max(1.5, 0.12 * dauer):
                return treffer.spot
        return None

    def verzeichnis(self, alle: bool = False) -> Verzeichnis:
        """Alle Signaturen zum Nachschlagen; verworfene nur mit `alle`. Wird erst neu gebaut, wenn sich etwas ändert."""
        if not alle and self._verzeichnis is not None:
            return self._verzeichnis
        zeilen = self._db.execute("select id, dauer_s, datei, abdruck from signaturen" + ("" if alle else " where urteil != 'verworfen'")).fetchall()
        spots = []
        for z in zeilen:
            packen = z["abdruck"]
            if packen is None:                    # vor dem Fingerabdruck aufgenommen: einmal nachholen
                try:
                    packen = _packen(*abdruecke(dekodieren(self.ton / z["datei"])))
                except KlangFehler:
                    continue
                self._db.execute("update signaturen set abdruck = ? where id = ?", (packen, z["id"]))
            spots.append((z["id"], *_auspacken(packen), z["dauer_s"]))
        v = Verzeichnis.aus(spots)
        if not alle:
            self._verzeichnis = v
        return v

    def treffer(self, kennung: str, quelle: str, name: str) -> Stand:
        """Die Signatur kam in dieser Quelle vor; liefert, ob sie dort wirkt. Eine neue Quelle ist erst ein Vorschlag."""
        jetzt = _jetzt()
        self._db.execute("insert into signatur_quellen (signatur, quelle, name, stand, treffer, zuerst, zuletzt)"
                         " values (?, ?, ?, 'vorschlag', 1, ?, ?) on conflict (signatur, quelle) do update set"
                         " treffer = treffer + 1, zuletzt = excluded.zuletzt, name = excluded.name", (kennung, quelle, name, jetzt, jetzt))
        return self._db.execute("select stand from signatur_quellen where signatur = ? and quelle = ?", (kennung, quelle)).fetchone()["stand"]

    def wirkt(self, kennung: str, quelle: str, ueberall: bool) -> bool:
        """Wirkt die Signatur in dieser Quelle (dort wird übersprungen oder stummgeschaltet)?"""
        s = self.eine(kennung)
        if s.urteil == "verworfen":
            return False
        if s.urteil == "bestaetigt" and ueberall:
            return True
        return any(q.quelle == quelle and q.stand == "wirkt" for q in s.quellen) and (s.urteil == "bestaetigt" or s.herkunft in SOFORT)

    def quelle_setzen(self, kennung: str, quelle: str, stand: Stand) -> Signatur:
        if self._db.execute("update signatur_quellen set stand = ? where signatur = ? and quelle = ?", (stand, kennung, quelle)).rowcount == 0:
            raise KeyError(quelle)
        return self.eine(kennung)

    def text_setzen(self, kennung: str, text: str, einschaetzung: str, werbender: str) -> None:
        """Abschrift und Einordnung. Ein Werbender, den der Sender selbst genannt hat, bleibt."""
        self._db.execute("update signaturen set text = ?, einschaetzung = ?, werbender = case when werbender = '' then ? else werbender end"
                         " where id = ?", (text, einschaetzung, werbender, kennung))

    def ohne_einordnung(self, grenze: int) -> list[Signatur]:
        """Noch nicht eingeordnet (ohne oder schon mit Abschrift), die neuesten zuerst."""
        return [self.eine(z["id"]) for z in self._db.execute("select id from signaturen where einschaetzung = '' and urteil = 'offen'"
                                                             " order by angelegt desc limit ?", (grenze,)).fetchall()]

    def eine(self, kennung: str) -> Signatur:
        zeile = self._db.execute("select * from signaturen where id = ?", (kennung,)).fetchone()
        if zeile is None:
            raise KeyError(kennung)
        quellen = self._db.execute("select quelle, name, stand, treffer, zuerst, zuletzt from signatur_quellen where signatur = ?"
                                   " order by treffer desc, name", (kennung,)).fetchall()
        return Signatur.model_validate({**{k: zeile[k] for k in zeile.keys() if k not in ("datei", "abdruck")},
                                        "quellen": [dict(q) for q in quellen]})

    def liste(self, urteil: Urteil | None = None, grenze: int = 200) -> list[Signatur]:
        sql = "select id from signaturen" + (" where urteil = ?" if urteil else "") + " order by angelegt desc limit ?"
        werte = (urteil, grenze) if urteil else (grenze,)
        return [self.eine(z["id"]) for z in self._db.execute(sql, werte).fetchall()]

    def faecher(self) -> dict[Fach, int]:
        zahlen: dict[Fach, int] = {"pruefen": 0, "wirkt": 0, "verworfen": 0}
        for z in self._db.execute(f"select {FACH_SQL} as fach, count(*) as n from signaturen s group by 1"):
            zahlen[z["fach"]] = z["n"]
        return zahlen

    def im_fach(self, fach: Fach, ab: int, anzahl: int) -> list[Signatur]:
        """Ein Stück eines Fachs: zu Prüfendes nach Aussicht, sonst was am öftesten lief."""
        rang = f"{RANG_SQL}, " if fach == "pruefen" else ""
        zeilen = self._db.execute(f"select s.id from signaturen s where {FACH_SQL} = ? order by {rang}{TREFFER_SQL} desc, s.angelegt desc, s.id"
                                  " limit ? offset ?", (fach, anzahl, ab)).fetchall()
        return [self.eine(z["id"]) for z in zeilen]

    def zahlen(self) -> dict[str, int]:
        zahlen = {"offen": 0, "bestaetigt": 0, "verworfen": 0}
        for z in self._db.execute("select urteil, count(*) as n from signaturen group by urteil"):
            zahlen[z["urteil"]] = z["n"]
        return zahlen

    def tondatei(self, kennung: str) -> Path:
        zeile = self._db.execute("select datei from signaturen where id = ?", (kennung,)).fetchone()
        if zeile is None:
            raise KeyError(kennung)
        return self.ton / zeile["datei"]

    def beurteilen(self, kennung: str, urteil: Urteil) -> Signatur:
        self.eine(kennung)
        self._db.execute("update signaturen set urteil = ?, beurteilt = ? where id = ?",
                         (urteil, None if urteil == "offen" else _jetzt(), kennung))
        if urteil == "bestaetigt":                      # bestätigt heißt: in den Quellen, in denen sie schon auffiel, wirkt sie
            self._db.execute("update signatur_quellen set stand = 'wirkt' where signatur = ?", (kennung,))
        self._verzeichnis = None
        return self.eine(kennung)
