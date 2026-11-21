"""Signaturen: Ausschnitte von Stellen, die ein Vergleich als eingesetzt gefunden hat (nur in einer Fassung).

Jede Signatur ist eine kurze Tondatei im Datenordner und eine Zeile in signaturen.db. Ein Mensch kann sie anhören und
bestätigen oder verwerfen (Irrtümer herausfiltern); verworfene bleiben mit ihrem Urteil stehen, damit derselbe Irrtum
nicht wieder als neu erscheint. Gelöscht wird hier nichts.
"""

from __future__ import annotations

import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

Urteil = Literal["offen", "bestaetigt", "verworfen"]

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

ENDUNG = ".m4a"


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


class SignaturFehler(Exception):
    pass


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

    def schliessen(self) -> None:
        self._db.close()

    def aufnehmen(self, ton: Path, von: float, bis: float, *, quelle: str, app: str, herkunft: str, fassung: str,
                  auftrag: str) -> Signatur:
        """Den Ausschnitt [von, bis) aus der Tondatei schneiden und als offene Signatur ablegen."""
        kennung = str(uuid.uuid4())
        datei = f"{kennung}{ENDUNG}"
        ziel = self.ton / datei
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{von:.2f}", "-to", f"{bis:.2f}", "-i", str(ton),
                            "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "96k", str(ziel)], capture_output=True)
        if r.returncode != 0 or not ziel.is_file():
            ziel.unlink(missing_ok=True)
            raise SignaturFehler(f"Ausschnitt nicht schneidbar: {r.stderr.decode(errors='replace').strip()[:300]}")
        self._db.execute(
            "insert into signaturen (id, quelle, app, herkunft, fassung, von_s, bis_s, dauer_s, datei, auftrag, angelegt)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kennung, quelle, app, herkunft, fassung, von, bis, round(bis - von, 2), datei, auftrag, _jetzt()))
        return self.eine(kennung)

    def eine(self, kennung: str) -> Signatur:
        zeile = self._db.execute("select * from signaturen where id = ?", (kennung,)).fetchone()
        if zeile is None:
            raise KeyError(kennung)
        return Signatur.model_validate({k: zeile[k] for k in zeile.keys() if k != "datei"})

    def liste(self, urteil: Urteil | None = None, grenze: int = 200) -> list[Signatur]:
        sql = "select id from signaturen" + (" where urteil = ?" if urteil else "") + " order by angelegt desc limit ?"
        werte = (urteil, grenze) if urteil else (grenze,)
        return [self.eine(z["id"]) for z in self._db.execute(sql, werte).fetchall()]

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
        return self.eine(kennung)
