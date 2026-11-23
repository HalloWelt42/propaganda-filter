"""Klang-Fingerabdruck: einen bekannten Spot in laufendem Ton wiedererkennen (HELF-256).

Verfahren: im Spektrum (mono, 8 kHz, alle 32 ms) die deutlichen Spitzen suchen und je zwei benachbarte zu einem Abdruck
verbinden (Frequenz, Abstand in der Frequenz, Abstand in der Zeit). Solche Paare überstehen Umkodieren, andere Lautstärke
und begrenzten Pegel, weil sie nur sagen, wo Spitzen liegen, nicht wie hoch sie sind. Ein bekannter Spot ist die Liste seiner
Abdrücke mit ihrer Zeit; im laufenden Ton zählt, wie viele Abdrücke zum selben Spot am selben zeitlichen Versatz passen.
Zufällige Treffer verteilen sich über alle Versätze, ein echter sammelt sie an einem.

Reine Rechnung ohne Zustand nach außen und ohne Netz; die Schwellen kommen vom Aufrufer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np

RATE = 8000
FENSTER = 1024
SCHRITT = 256                      # 32 ms
ZEILE_S = SCHRITT / RATE
NACHBAR_ZEIT, NACHBAR_FREQUENZ = 6, 9     # eine Spitze ist die höchste in diesem Umkreis (Zeilen, Bänder)
BAND_VON, BAND_BIS = 8, 480               # 62 Hz bis 3750 Hz
ZWEITE_AB, ZWEITE_BIS = 2, 62             # der zweite Punkt eines Paars liegt so viele Zeilen später
ZWEITE_FREQUENZ = 63                      # und höchstens so viele Bänder daneben
PAARE = 6                                 # so viele Paare beginnt eine Spitze höchstens


def _hoechste(m: np.ndarray, weite: int, achse: int) -> np.ndarray:
    """Gleitendes Maximum über ±weite entlang der Achse."""
    aus = m.copy()
    for k in range(1, weite + 1):
        vor = np.roll(m, k, axis=achse)
        nach = np.roll(m, -k, axis=achse)
        rand = [slice(None)] * 2
        rand[achse] = slice(0, k)
        vor[tuple(rand)] = -np.inf
        rand[achse] = slice(-k, None)
        nach[tuple(rand)] = -np.inf
        np.maximum(aus, vor, out=aus)
        np.maximum(aus, nach, out=aus)
    return aus


def spitzen(x: np.ndarray) -> np.ndarray:
    """Zeilen (Zeit, Band) der Spitzen, nach Zeit geordnet."""
    if len(x) < FENSTER:
        return np.empty((0, 2), dtype=np.int64)
    n = 1 + (len(x) - FENSTER) // SCHRITT
    rahmen = np.lib.stride_tricks.as_strided(x, (n, FENSTER), (x.strides[0] * SCHRITT, x.strides[0]))
    spektrum = np.abs(np.fft.rfft(rahmen * np.hanning(FENSTER), axis=1))[:, :BAND_BIS]
    log = np.log(spektrum + 1e-6)
    spitze = log == _hoechste(_hoechste(log, NACHBAR_ZEIT, 0), NACHBAR_FREQUENZ, 1)
    spitze[:, :BAND_VON] = False
    # nur was sich aus seiner Umgebung hebt: über dem mittleren Pegel des Bands der letzten Sekunden und der Zeile
    boden = np.maximum(log.mean(axis=1, keepdims=True), log.max() - 11.0)
    spitze &= log > boden + 0.5
    return np.argwhere(spitze)


def abdruecke(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(Abdrücke, Zeilen): je Paar von Spitzen eine Zahl und die Zeile der ersten Spitze."""
    p = spitzen(x)
    if len(p) < 2:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    zeit, band = p[:, 0], p[:, 1]
    hash_teile, zeit_teile = [], []
    offen = np.zeros(len(p), dtype=np.int64)
    for k in range(1, len(p)):
        dt = zeit[k:] - zeit[:-k]
        if not len(dt) or dt.min() > ZWEITE_BIS:
            break
        df = band[k:] - band[:-k]
        gut = (dt >= ZWEITE_AB) & (dt <= ZWEITE_BIS) & (np.abs(df) <= ZWEITE_FREQUENZ) & (offen[:-k] < PAARE)
        i = np.flatnonzero(gut)
        if len(i):
            offen[i] += 1
            hash_teile.append((band[i] << 13) | ((df[i] + ZWEITE_FREQUENZ) << 6) | dt[i])
            zeit_teile.append(zeit[i])
        if k > 400:
            break
    if not hash_teile:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    h, t = np.concatenate(hash_teile), np.concatenate(zeit_teile)
    ordnung = np.argsort(t, kind="stable")
    return h[ordnung], t[ordnung]


@dataclass(frozen=True)
class Treffer:
    """Ein Spot im Ton: `beginn` ist die Sekunde, an der er im Ton anfing (kann vor dem Gehörten liegen)."""

    spot: str
    beginn: float
    punkte: int
    anteil: float


@dataclass
class Verzeichnis:
    """Bekannte Spots, nach Abdruck geordnet, damit ein Nachschlagen nicht mit ihrer Zahl wächst."""

    spots: list[str] = field(default_factory=list)
    laengen: list[float] = field(default_factory=list)
    _hash: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    _spot: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))
    _zeit: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))

    @classmethod
    def aus(cls, spots: Iterable[tuple[str, np.ndarray, np.ndarray, float]]) -> Verzeichnis:
        v = cls()
        h, s, t = [], [], []
        for name, abdruck, zeit, dauer in spots:
            s.append(np.full(len(abdruck), len(v.spots), np.int64))
            h.append(abdruck)
            t.append(zeit)
            v.spots.append(name)
            v.laengen.append(dauer)
        if h:
            alle = np.concatenate(h)
            ordnung = np.argsort(alle, kind="stable")
            v._hash, v._spot, v._zeit = alle[ordnung], np.concatenate(s)[ordnung], np.concatenate(t)[ordnung]
        return v

    def suchen(self, h: np.ndarray, t: np.ndarray, *, mindestens: int, anteil: float) -> list[Treffer]:
        """Spots, die in diesen Abdrücken am selben Versatz auffallen. `mindestens`: so viele müssen passen; `anteil`: dieser
        Teil der Abdrücke des Stücks muss dazugehören (so zählt ein dichtes Stück nicht leichter als ein ruhiges)."""
        if not len(h) or not len(self._hash):
            return []
        von = np.searchsorted(self._hash, h, "left")
        bis = np.searchsorted(self._hash, h, "right")
        zahl = bis - von
        if not zahl.any():
            return []
        frage = np.repeat(np.arange(len(h)), zahl)
        lauf = np.arange(zahl.sum()) - np.repeat(np.cumsum(zahl) - zahl, zahl)
        stelle = np.repeat(von, zahl) + lauf
        spot = self._spot[stelle]
        versatz = t[frage] - self._zeit[stelle]
        # benachbarte Versätze (± 1 Zeile) gehören zusammen: das Raster des Stroms liegt nie genau auf dem des Spots
        schluessel = spot * (1 << 32) + (versatz + (1 << 24))
        werte, anzahl = np.unique(schluessel, return_counts=True)
        je = dict(zip(werte.tolist(), anzahl.tolist()))
        beste: dict[int, tuple[int, int]] = {}
        for w, n in zip(werte.tolist(), anzahl.tolist()):
            summe = n + je.get(w - 1, 0) + je.get(w + 1, 0)
            s, v = w >> 32, (w & ((1 << 32) - 1)) - (1 << 24)
            if summe > beste.get(s, (0, 0))[0]:
                beste[s] = (summe, v)
        aus = [Treffer(self.spots[s], v * ZEILE_S, n, n / len(h)) for s, (n, v) in beste.items()
               if n >= mindestens and n / len(h) >= anteil]
        return sorted(aus, key=lambda x: -x.punkte)
