"""Zwei Fassungen derselben Folge am Klang ausrichten und die Abschnitte nennen, die nur in einer vorkommen.

Verfahren (erprobt an echten Folgen, RAD-32): dekodieren (mono, 8 kHz), je 10 ms die Energie in 24 Bändern (log),
die Fassung A in Stücke von 5 s teilen und jedes Stück in B per Korrelation suchen (Pearson über Zeit und Bänder, über
FFT). Erst am erwarteten Versatz, nur wenn es dort nicht passt, über die ganze Fassung. Danach werden die gemeinsamen
Läufe auf 10 ms genau begrenzt; was dazwischen liegt, kommt nur in A vor.

Reine Rechnung ohne Zustand und ohne Netz: Eingabe sind Merkmale (oder eine Datei für `dekodieren`), Ausgabe ein `Bild`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RATE = 8000
FENSTER = 512
SCHRITT = 80                 # 10 ms: feiner als die Silben, sonst fällt die Güte zwischen den Rastern
STUECK = 500                 # 5 s
BAENDER = np.geomspace(150, 3800, 25)
GLAETTEN = 5                 # Zeilen (50 ms), über die der Abstand beim Begrenzen gemittelt wird
ZEILE_S = SCHRITT / RATE
STUECK_S = STUECK * ZEILE_S

Fortschritt = Callable[[float], None]


class KlangFehler(Exception):
    """Die Datei ist kein lesbarer Ton oder zu kurz für einen Vergleich."""


@dataclass(frozen=True)
class Stelle:
    von: float
    bis: float

    @property
    def dauer(self) -> float:
        return self.bis - self.von


@dataclass(frozen=True)
class Lauf:
    """Ein Abschnitt von A, der in B am Versatz (Sekunden, B minus A) vorkommt."""

    von: float
    bis: float
    versatz: float


@dataclass(frozen=True)
class Bild:
    """Wie A in B vorkommt."""

    dauer: float
    gemeinsam: list[Lauf]
    nur_hier: list[Stelle]

    @property
    def anteil_gemeinsam(self) -> float:
        return sum(l.bis - l.von for l in self.gemeinsam) / self.dauer if self.dauer else 0.0


def dekodieren(pfad: Path) -> np.ndarray:
    try:
        roh = subprocess.run(["ffmpeg", "-v", "error", "-i", str(pfad), "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
                             capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as f:
        raise KlangFehler(f"Ton nicht lesbar: {f.stderr.decode(errors='replace').strip()[:300]}") from f
    return np.frombuffer(roh, dtype=np.float32)


def merkmale(x: np.ndarray) -> np.ndarray:
    """Je 10 ms die Energie in 24 Bändern (log10)."""
    if len(x) < FENSTER + 2 * STUECK * SCHRITT:
        raise KlangFehler(f"Ton zu kurz für einen Vergleich (mindestens {2 * STUECK_S:.0f} s)")
    n = 1 + (len(x) - FENSTER) // SCHRITT
    rahmen = np.lib.stride_tricks.as_strided(x, (n, FENSTER), (x.strides[0] * SCHRITT, x.strides[0]))
    frequenz = np.fft.rfftfreq(FENSTER, 1 / RATE)
    fenster = np.hanning(FENSTER)
    teile = []
    for anfang in range(0, n, 20000):  # in Teilen, damit lange Folgen nicht das ganze Spektrum auf einmal halten
        spektrum = np.abs(np.fft.rfft(rahmen[anfang:anfang + 20000] * fenster, axis=1)) ** 2
        teile.append(np.stack([spektrum[:, (frequenz >= a) & (frequenz < b)].sum(axis=1)
                               for a, b in zip(BAENDER[:-1], BAENDER[1:])], axis=1))
    return np.log10(np.concatenate(teile) + 1e-9).astype(np.float64)


class _Suche:
    """Stücke aus A in B finden; die FFT von B wird einmal gerechnet."""

    def __init__(self, b: np.ndarray):
        self.b = b
        self.laenge = 1 << int(np.ceil(np.log2(len(b) + STUECK)))
        self.fb = np.fft.rfft(b, self.laenge, axis=0)
        self.s1 = np.concatenate([[0], np.cumsum(b.sum(axis=1))])
        self.s2 = np.concatenate([[0], np.cumsum((b ** 2).sum(axis=1))])

    def _nenner(self, t: np.ndarray) -> np.ndarray:
        werte = STUECK * self.b.shape[1]
        s = self.s1[t + STUECK] - self.s1[t]
        q = self.s2[t + STUECK] - self.s2[t]
        return np.sqrt(np.maximum(q - s * s / werte, 1e-12))

    def guete(self, c: np.ndarray, t: int) -> float:
        if t < 0 or t + STUECK > len(self.b):
            return -1.0
        cz = c - c.mean()
        z = float((cz * self.b[t:t + STUECK]).sum())
        return z / (np.linalg.norm(cz) * self._nenner(np.array([t]))[0] + 1e-12)

    def ueberall(self, c: np.ndarray) -> tuple[int, float]:
        cz = c - c.mean()
        fc = np.fft.rfft(cz, self.laenge, axis=0)
        korr = np.fft.irfft((self.fb * np.conj(fc)).sum(axis=1), self.laenge)
        t = np.arange(len(self.b) - STUECK + 1)
        werte = korr[t] / (np.linalg.norm(cz) * self._nenner(t) + 1e-12)
        i = int(np.argmax(werte))
        return i, float(werte[i])


def _grobe_laeufe(a: np.ndarray, b: np.ndarray, gleich: float, fortschritt: Fortschritt) -> list[tuple[float, float, int | None]]:
    """Je Stück von A: in B gefunden (Versatz in Zeilen) oder nicht (None); gleiche Nachbarn zusammengefasst."""
    suche = _Suche(b)
    versatz = 0
    laeufe: list[tuple[float, float, int | None]] = []
    anfaenge = range(0, len(a) - STUECK + 1, STUECK)
    for i, start in enumerate(anfaenge):
        c = a[start:start + STUECK]
        g = suche.guete(c, start + versatz)
        if g < gleich:
            t, g2 = suche.ueberall(c)
            if g2 >= gleich:
                versatz, g = t - start, g2
        art = versatz if g >= gleich else None
        von, bis = start * ZEILE_S, start * ZEILE_S + STUECK_S
        if laeufe and laeufe[-1][2] == art:
            laeufe[-1] = (laeufe[-1][0], bis, art)
        else:
            laeufe.append((von, bis, art))
        fortschritt((i + 1) / len(anfaenge))
    return laeufe


def _abstand(a: np.ndarray, b: np.ndarray, versatz: int) -> np.ndarray:
    """Abstand je Zeile von A zur Zeile von B am Versatz (Mittel über die Bänder, je Fassung ohne ihren Bandpegel),
    geglättet; außerhalb von B groß."""
    an, bn = a - a.mean(axis=0), b - b.mean(axis=0)
    d = np.full(len(a), 10.0)
    t = np.arange(len(a))
    gueltig = (t + versatz >= 0) & (t + versatz < len(b))
    d[gueltig] = np.abs(an[t[gueltig]] - bn[t[gueltig] + versatz]).mean(axis=1)
    return np.convolve(d, np.ones(GLAETTEN) / GLAETTEN, mode="same")


def _begrenzen(a: np.ndarray, b: np.ndarray, von: float, bis: float, versatz: int) -> Lauf:
    """Einen groben gemeinsamen Lauf auf 10 ms genau begrenzen: vom sicheren Kern aus so weit nach außen, wie der
    Abstand unter der Schwelle bleibt (Schwelle aus dem Kern selbst)."""
    zeile = lambda s: int(round(s / ZEILE_S))
    innen = slice(zeile(von) + STUECK // 2, max(zeile(von) + STUECK // 2 + 1, zeile(bis) - STUECK // 2))
    # Versatz auf die Zeile genau: der mit dem kleinsten Abstand im Kern
    k, d = min(((k, _abstand(a, b, versatz + k)) for k in range(-3, 4)), key=lambda x: float(np.median(x[1][innen])))
    schwelle = max(0.35, 4 * float(np.median(d[innen])))
    anfang = zeile(von) + STUECK // 2
    while anfang > 0 and d[anfang - 1] < schwelle:
        anfang -= 1
    ende = min(len(a), zeile(bis) - STUECK // 2)
    while ende < len(a) and d[ende] < schwelle:
        ende += 1
    return Lauf(round(anfang * ZEILE_S, 2), round(ende * ZEILE_S, 2), round((versatz + k) * ZEILE_S, 2))


def vereinigen(listen: list[list[Stelle]], luecke_s: float = 0.0) -> list[Stelle]:
    """Stellen aus mehreren Vergleichen derselben Fassung zusammen: was in irgendeinem Vergleich nur hier vorkommt,
    gehört dazu; überlappende oder höchstens `luecke_s` auseinanderliegende Stellen werden ein Abschnitt."""
    alle = sorted((s for l in listen for s in l), key=lambda s: s.von)
    zusammen: list[Stelle] = []
    for s in alle:
        if zusammen and s.von <= zusammen[-1].bis + luecke_s:
            zusammen[-1] = Stelle(zusammen[-1].von, max(zusammen[-1].bis, s.bis))
        else:
            zusammen.append(s)
    return zusammen


def vergleichen(a: np.ndarray, b: np.ndarray, *, gleich: float = 0.85, mindestens_s: float = 3.0,
                fortschritt: Fortschritt = lambda _: None) -> Bild:
    """Wie A (Merkmale) in B vorkommt: gemeinsame Läufe auf 10 ms genau und dazwischen, was nur in A ist."""
    gemeinsam = [_begrenzen(a, b, von, bis, art) for von, bis, art in _grobe_laeufe(a, b, gleich, fortschritt)
                 if art is not None and bis - von >= 2 * STUECK_S]
    dauer = len(a) * ZEILE_S
    nur_hier, zuletzt = [], 0.0
    for lauf in gemeinsam:
        if lauf.von - zuletzt >= mindestens_s:
            nur_hier.append(Stelle(round(zuletzt, 2), lauf.von))
        zuletzt = max(zuletzt, lauf.bis)
    if dauer - zuletzt >= mindestens_s:
        nur_hier.append(Stelle(round(zuletzt, 2), round(dauer, 2)))
    return Bild(round(dauer, 2), gemeinsam, nur_hier)
