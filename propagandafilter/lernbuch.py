"""Gedächtnis des Lernlaufs (lernlauf.db): welche Sender, wann gehört, was gefunden, dazu die Abdrücke der letzten Tage."""

from __future__ import annotations

import sqlite3
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from .wiederholung import Aufnahme

Grund = Literal["favorit", "gehoert", "aehnlich", "bekannt"]
GEWICHT: dict[str, float] = {"gehoert": 5.0, "favorit": 4.0, "aehnlich": 2.0, "bekannt": 1.0}
PAUSE_H = 18.0               # denselben Sender frühestens danach wieder
NACH_FEHLER_H = 6.0
NIE_H = 24.0 * 14            # wer noch nie dran war, gilt als so lange nicht gehört

SCHEMA = """
create table if not exists sender (
  quelle text primary key, name text not null, adresse text not null, grund text not null, gewicht real not null default 1,
  dabei integer not null default 1,      -- steht auf der Liste der App
  aus integer not null default 0,        -- vom Menschen ausgenommen
  zuletzt text, sekunden real not null default 0, kandidaten integer not null default 0, bekannte integer not null default 0,
  fehler text not null default '', fehler_am text
);
create table if not exists aufnahmen (
  id text primary key, quelle text not null, name text not null, begonnen text not null, dauer real not null, abdruck blob not null
);
create index if not exists aufnahmen_begonnen on aufnahmen(begonnen);
create table if not exists laeufe (
  id text primary key, begonnen text not null, beendet text, sender integer not null default 0, fehler integer not null default 0,
  sekunden real not null default 0, neu integer not null default 0, bekannte integer not null default 0
);
"""


def jetzt() -> datetime:
    return datetime.now().astimezone()


def _text(zeit: datetime) -> str:
    return zeit.isoformat(timespec="seconds")


class Lernsender(BaseModel):
    """Ein Sender, den die App zum Hören vorschlägt."""

    quelle: str = Field(min_length=1, max_length=200, description="Kennung der Quelle bei der App, etwa die uuid des Senders")
    name: str = Field("", max_length=300)
    adresse: str = Field(pattern=r"^https?://\S+$", max_length=2000)
    grund: Grund
    gewicht: float = Field(1.0, ge=0, le=100, description="innerhalb des Grunds, etwa wie oft gehört")


class Senderstand(Lernsender):
    dabei: bool
    aus: bool
    zuletzt: str | None
    stunden: float
    kandidaten: int = Field(description="neue Stücke, die dieser Sender lieferte")
    bekannte: int = Field(description="wie oft hier schon bekannte Werbung lief")
    fehler: str


class Senderseite(BaseModel):
    """Ein Stück der Liste und wie viele Sender sie hat."""

    gesamt: int
    sender: list[Senderstand]


class Lauf(BaseModel):
    id: str
    begonnen: str
    beendet: str | None
    sender: int
    fehler: int
    stunden: float
    neu: int
    bekannte: int


@dataclass(frozen=True)
class Zahlen:
    sender: int
    stunden: float
    archiv_stunden: float


class Lernbuch:
    def __init__(self, ordner: Path) -> None:
        ordner.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(ordner / "lernlauf.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)
        self._db.execute("update laeufe set beendet = begonnen where beendet is null")      # vom letzten Mal abgebrochen

    def schliessen(self) -> None:
        self._db.close()

    # ----- Sender

    def sender_setzen(self, sender: list[Lernsender]) -> None:
        """Die Liste der App. Neue kommen dazu, vorhandene bekommen Grund und Gewicht neu, fehlende ruhen mit ihren Zahlen."""
        self._db.execute("begin")
        try:
            self._db.execute("update sender set dabei = 0")
            self._db.executemany("insert into sender (quelle, name, adresse, grund, gewicht) values (?, ?, ?, ?, ?) on conflict (quelle) do update"
                                 " set name = excluded.name, adresse = excluded.adresse, grund = excluded.grund, gewicht = excluded.gewicht, dabei = 1",
                                 [(s.quelle, s.name, s.adresse, s.grund, s.gewicht) for s in sender])
            self._db.execute("commit")
        except BaseException:
            self._db.execute("rollback")
            raise

    def ausnehmen(self, quelle: str, aus: bool) -> None:
        if self._db.execute("update sender set aus = ? where quelle = ?", (int(aus), quelle)).rowcount == 0:
            raise KeyError(quelle)

    def sender(self, ab: int = 0, anzahl: int = 1000) -> Senderseite:
        """Die Sender der Liste, die mit den meisten Funden zuerst."""
        gesamt = self._db.execute("select count(*) from sender where dabei = 1").fetchone()[0]
        zeilen = self._db.execute("select * from sender where dabei = 1 order by kandidaten + bekannte desc, zuletzt is null, name, quelle limit ? offset ?",
                                  (anzahl, ab)).fetchall()
        return Senderseite(gesamt=gesamt, sender=[self._senderstand(z) for z in zeilen])

    def einer(self, quelle: str) -> Senderstand:
        z = self._db.execute("select * from sender where quelle = ?", (quelle,)).fetchone()
        if z is None:
            raise KeyError(quelle)
        return self._senderstand(z)

    @staticmethod
    def _senderstand(z: sqlite3.Row) -> Senderstand:
        return Senderstand(quelle=z["quelle"], name=z["name"], adresse=z["adresse"], grund=z["grund"], gewicht=z["gewicht"], dabei=bool(z["dabei"]),
                           aus=bool(z["aus"]), zuletzt=z["zuletzt"], stunden=round(z["sekunden"] / 3600, 2), kandidaten=z["kandidaten"],
                           bekannte=z["bekannte"], fehler=z["fehler"])

    def naechste(self, anzahl: int, ohne: set[str] | frozenset[str] = frozenset()) -> list[Lernsender]:
        """Die wichtigsten, die am längsten nicht dran waren; wer Werbung lieferte, rückt etwas vor."""
        t, wahl = jetzt(), []
        stunden = lambda zeit, sonst: (t - datetime.fromisoformat(zeit)).total_seconds() / 3600 if zeit else sonst
        for z in self._db.execute("select * from sender where dabei = 1 and aus = 0"):
            seit = min(stunden(z["zuletzt"], NIE_H), NIE_H)
            if z["quelle"] in ohne or seit < PAUSE_H or stunden(z["fehler_am"], NIE_H) < NACH_FEHLER_H:
                continue
            wert = GEWICHT[z["grund"]] * (1 + min(z["gewicht"], 20) / 10) * seit / 24 * (1 + 0.1 * min(z["kandidaten"] + z["bekannte"], 10))
            wahl.append((-wert, z["name"], z))
        return [Lernsender(**{k: z[k] for k in ("quelle", "name", "adresse", "grund", "gewicht")}) for *_, z in sorted(wahl, key=lambda x: x[:2])[:max(anzahl, 0)]]

    # ----- Läufe

    def lauf_beginnen(self) -> str:
        kennung = str(uuid.uuid4())
        self._db.execute("insert into laeufe (id, begonnen) values (?, ?)", (kennung, _text(jetzt())))
        return kennung

    def lauf_beenden(self, lauf: str) -> None:
        self._db.execute("update laeufe set beendet = ? where id = ?", (_text(jetzt()), lauf))

    def gehoert(self, lauf: str, quelle: str, sekunden: float, neu: int, bekannte: int) -> None:
        self._db.execute("update sender set zuletzt = ?, sekunden = sekunden + ?, kandidaten = kandidaten + ?, bekannte = bekannte + ?, fehler = '',"
                         " fehler_am = null where quelle = ?", (_text(jetzt()), sekunden, neu, bekannte, quelle))
        self._db.execute("update laeufe set sender = sender + 1, sekunden = sekunden + ?, neu = neu + ?, bekannte = bekannte + ? where id = ?",
                         (sekunden, neu, bekannte, lauf))

    def gescheitert(self, lauf: str, quelle: str, fehler: str) -> None:
        self._db.execute("update sender set fehler = ?, fehler_am = ? where quelle = ?", (fehler[:300], _text(jetzt()), quelle))
        self._db.execute("update laeufe set fehler = fehler + 1 where id = ?", (lauf,))

    def laeufe(self, grenze: int = 14) -> list[Lauf]:
        return [Lauf(**{k: z[k] for k in ("id", "begonnen", "beendet", "sender", "fehler", "neu", "bekannte")}, stunden=round(z["sekunden"] / 3600, 2))
                for z in self._db.execute("select * from laeufe order by begonnen desc limit ?", (grenze,))]

    def zahlen(self) -> Zahlen:
        n, s = self._db.execute("select count(*), coalesce(sum(sekunden), 0) from sender where dabei = 1 and aus = 0").fetchone()
        a = self._db.execute("select coalesce(sum(dauer), 0) from aufnahmen").fetchone()[0]
        return Zahlen(n, round(s / 3600, 2), round(a / 3600, 2))

    # ----- Abdrücke der letzten Tage (der Ton selbst bleibt nie)

    def merken(self, a: Aufnahme, name: str) -> None:
        daten = zlib.compress(np.stack([a.h, a.t]).astype(np.int32).tobytes(), 6)
        self._db.execute("insert into aufnahmen (id, quelle, name, begonnen, dauer, abdruck) values (?, ?, ?, ?, ?, ?)",
                         (a.name, a.quelle, name, _text(jetzt()), a.dauer, daten))

    def archiv(self) -> list[Aufnahme]:
        aus = []
        for z in self._db.execute("select id, quelle, dauer, abdruck from aufnahmen order by begonnen"):
            beide = np.frombuffer(zlib.decompress(z["abdruck"]), np.int32).reshape(2, -1).astype(np.int64)
            aus.append(Aufnahme(z["id"], z["quelle"], z["dauer"], beide[0], beide[1]))
        return aus

    def vergessen(self, stunden: float) -> None:
        """Was länger als so lange her ist, dient keinem Vergleich mehr."""
        self._db.execute("delete from aufnahmen where begonnen < ?", (_text(jetzt() - timedelta(hours=stunden)),))

    def name(self, quelle: str) -> str:
        z = self._db.execute("select name from sender where quelle = ?", (quelle,)).fetchone()
        return z["name"] if z else quelle
