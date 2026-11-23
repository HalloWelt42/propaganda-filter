"""Wiederholungen finden (Lernlauf, HELF-256): Werbung läuft immer wieder, dieselbe Aufnahme bei einem Sender mehrmals und oft
auch bei anderen. Ein Stück Ton, das an mehreren Stellen vorkommt und so lang ist wie ein Spot, ist ein Kandidat.

Musik wiederholt sich auch, ist aber länger als ein Spot; Kennungen des Senders sind kürzer. Was an den Rand einer Aufnahme
stößt, zählt nicht, denn dort ist seine Länge unbekannt. Ob ein Kandidat Werbung ist, entscheidet hier niemand.

Reine Rechnung: Eingabe sind Abdrücke ganzer Aufnahmen (ausgedünnt), Ausgabe Stellen mit ihren Vorkommen.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .fingerabdruck import ZEILE_S

AUSDUENNEN = 3                 # jeder dritte Abdruck genügt: der Vergleich geht über viele Stunden
BLOCK_S = 5.0                  # in solchen Stücken wird gesucht
SCHRITT_S = 2.5
PUNKTE = 14                    # so viele (ausgedünnte) Abdrücke eines Stücks müssen an einem Versatz liegen
ANTEIL = 0.05
LUECKE_S = 4.0                 # so viel darf in einem Lauf fehlen (Sprache über Musik, kurze Störung)
ENDE_S = 0.6                   # ein Abdruck steht an seiner ersten Spitze, das Stück reicht im Mittel so viel weiter
SELBST_ABSTAND_S = 600.0       # innerhalb einer Aufnahme erst ab diesem Abstand eine Wiederholung (ein Lied wiederholt sich selbst)
SCHLEIFE_NAH_S, SCHLEIFE_VERSATZ_S = 60.0, 40.0     # so nah beieinander mit verschiedenem Versatz: Musik aus Schleifen, kein Spot


@dataclass(frozen=True)
class Aufnahme:
    """Eine Aufnahme im Vergleich: `name` ist ihre Kennung, `quelle` der Sender."""

    name: str
    quelle: str
    dauer: float
    h: np.ndarray
    t: np.ndarray


@dataclass(frozen=True)
class Vorkommen:
    aufnahme: str
    quelle: str
    von: float
    bis: float


@dataclass(frozen=True)
class Kandidat:
    """Ein Stück der neuen Aufnahme, das auch anderswo vorkommt."""

    von: float
    bis: float
    vorkommen: tuple[Vorkommen, ...]

    @property
    def dauer(self) -> float:
        return self.bis - self.von


def ausduennen(h: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dieselbe Auswahl in jeder Aufnahme: was bleibt, hängt nur vom Abdruck selbst ab."""
    bleibt = (h * 2654435761 >> 7) % AUSDUENNEN == 0
    return h[bleibt], t[bleibt]


class Archiv:
    """Aufnahmen zum Nachschlagen (auch die neue selbst gehört hinein)."""

    def __init__(self, aufnahmen: list[Aufnahme]) -> None:
        self.aufnahmen = aufnahmen
        n = [len(a.h) for a in aufnahmen]
        alle = np.concatenate([a.h for a in aufnahmen]) if aufnahmen else np.empty(0, np.int64)
        wer = np.repeat(np.arange(len(aufnahmen)), n)
        zeit = np.concatenate([a.t for a in aufnahmen]) if aufnahmen else np.empty(0, np.int64)
        ordnung = np.argsort(alle, kind="stable")
        self.h, self.wer, self.t = alle[ordnung], wer[ordnung], zeit[ordnung]
        # Abdrücke, die überall vorkommen (Stille, Brummen), sagen nichts
        werte, anfang, zahl = np.unique(self.h, return_index=True, return_counts=True)
        self._haeufig = set(werte[zahl > max(60, 6 * len(aufnahmen))].tolist())

    def gleiche(self, h: np.ndarray, t: np.ndarray) -> list[tuple[int, int, int, int, int]]:
        """Für die Abdrücke eines Stücks: (Aufnahme, Versatz, Punkte, erste Zeile, letzte Zeile), wo es auch vorkommt."""
        if self._haeufig:
            bleibt = np.array([x not in self._haeufig for x in h.tolist()], dtype=bool)
            h, t = h[bleibt], t[bleibt]
        if len(h) < PUNKTE:
            return []
        von, bis = np.searchsorted(self.h, h, "left"), np.searchsorted(self.h, h, "right")
        zahl = bis - von
        frage = np.repeat(np.arange(len(h)), zahl)
        stelle = np.repeat(von, zahl) + np.arange(zahl.sum()) - np.repeat(np.cumsum(zahl) - zahl, zahl)
        wer, versatz, zeile = self.wer[stelle], self.t[stelle] - t[frage], t[frage]
        schluessel = wer * (1 << 32) + (versatz + (1 << 24)) // 3          # Versätze in Schritten von 3 Zeilen (0,1 s)
        ordnung = np.argsort(schluessel, kind="stable")
        s, z, w, v = schluessel[ordnung], zeile[ordnung], wer[ordnung], versatz[ordnung]
        grenzen = np.flatnonzero(np.r_[True, s[1:] != s[:-1], True])
        aus = []
        for a, b in zip(grenzen[:-1], grenzen[1:]):
            # der Nachbarschritt gehört dazu, wenn der Versatz auf der Grenze liegt
            n = b - a
            if n >= PUNKTE // 2 and n >= ANTEIL * len(h) / 2:
                aus.append((int(w[a]), int(np.median(v[a:b])), int(n), int(z[a:b].min()), int(z[a:b].max())))
        zusammen: dict[tuple[int, int], list[int]] = {}
        for wer_, ver, n, lo, hi in sorted(aus):
            schl = next((k for k in ((wer_, ver + d) for d in range(-4, 5)) if k in zusammen), None)
            if schl:
                e = zusammen[schl]
                e[0] += n
                e[1], e[2] = min(e[1], lo), max(e[2], hi)
            else:
                zusammen[(wer_, ver)] = [n, lo, hi]
        return [(w_, ver, e[0], e[1], e[2]) for (w_, ver), e in zusammen.items() if e[0] >= PUNKTE and e[0] >= ANTEIL * len(h)]


def suchen(neu: Aufnahme, archiv: Archiv, *, mindestens_s: float = 8.0, hoechstens_s: float = 65.0) -> list[Kandidat]:
    """Stücke von `neu`, die im Archiv (auch in `neu` selbst) noch einmal vorkommen und so lang sind wie ein Spot."""
    laeufe: dict[tuple[int, int], list[list[float]]] = defaultdict(list)
    block, schritt = int(BLOCK_S / ZEILE_S), int(SCHRITT_S / ZEILE_S)
    ich = next(i for i, a in enumerate(archiv.aufnahmen) if a.name == neu.name)
    for ab in range(0, int(neu.dauer / ZEILE_S) - block + 1, schritt):
        a, b = np.searchsorted(neu.t, ab), np.searchsorted(neu.t, ab + block)
        for wer, versatz, _n, lo, hi in archiv.gleiche(neu.h[a:b], neu.t[a:b]):
            if wer == ich and abs(versatz) * ZEILE_S < SELBST_ABSTAND_S:
                continue
            schl = next((k for k in ((wer, versatz + d) for d in range(-6, 7)) if k in laeufe), (wer, versatz))
            von, bis = lo * ZEILE_S, hi * ZEILE_S + ENDE_S
            reihe = laeufe[schl]
            if reihe and von - reihe[-1][1] <= LUECKE_S:
                reihe[-1][1] = max(reihe[-1][1], bis)
            else:
                reihe.append([von, bis])
    # was zu einem langen gemeinsamen Stück gehört, ist ein Lied oder ein Beitrag, auch wenn Teile davon allein auffallen
    lang = [(von, bis) for reihe in laeufe.values() for von, bis in reihe if bis - von > hoechstens_s]
    im_langen = lambda von, bis: any(min(b, bis) - max(a, von) > 0.5 * (bis - von) for a, b in lang)
    alle = [(von, bis, wer, versatz * ZEILE_S) for (wer, versatz), reihe in laeufe.items() for von, bis in reihe if bis - von >= mindestens_s / 2]
    stuecke: list[tuple[float, float, Vorkommen]] = []
    for von, bis, wer, versatz in alle:
        a = archiv.aufnahmen[wer]
        dort = (von + versatz, bis + versatz)
        rand = von < 2 or bis > neu.dauer - 2 or dort[0] < 2 or dort[1] > a.dauer - 2
        if rand or not mindestens_s <= bis - von <= hoechstens_s or im_langen(von, bis) or _schleife(von, bis, wer, versatz, alle):
            continue
        stuecke.append((von, bis, Vorkommen(a.name, a.quelle, round(dort[0], 2), round(dort[1], 2))))
    return _zusammen(stuecke)


def _schleife(von: float, bis: float, wer: int, versatz: float, alle: list[tuple[float, float, int, float]]) -> bool:
    """Passt dieselbe Gegend auch leicht verschoben zur selben Aufnahme, ist es Musik aus wiederholten Takten (Elektronik)."""
    return any(w == wer and 0.5 < abs(ver - versatz) < SCHLEIFE_VERSATZ_S and max(a, von) - min(b, bis) < SCHLEIFE_NAH_S
               for a, b, w, ver in alle)


def _zusammen(stuecke: list[tuple[float, float, Vorkommen]]) -> list[Kandidat]:
    """Vorkommen desselben Stücks sammeln: gleich sind Stücke, die sich zum größten Teil decken."""
    gruppen: list[list[tuple[float, float, Vorkommen]]] = []
    for s in sorted(stuecke, key=lambda s: (s[0], s[1])):
        for g in gruppen:
            v, b = float(np.median([x[0] for x in g])), float(np.median([x[1] for x in g]))
            deckung = min(b, s[1]) - max(v, s[0])
            if deckung > 0.75 * max(b - v, s[1] - s[0]):
                g.append(s)
                break
        else:
            gruppen.append([s])
    return [Kandidat(round(float(np.median([x[0] for x in g])), 2), round(float(np.median([x[1] for x in g])), 2),
                     tuple(x[2] for x in g)) for g in gruppen]
