"""Einstellungen des Filters: eine TOML-Datei im Datenordner, die der Mensch am Mac pflegt. Die Apps können sie nicht
ändern. Geänderte Dateien gelten ab dem nächsten Auftrag; ist eine neue Fassung ungültig, gilt die letzte gültige
weiter und der Fehler steht im Stand (GET /api/v1/stand)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, field_validator, model_validator


class _Modell(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KellerwolkeEinstellungen(_Modell):
    adresse: str = Field("https://wolke.pi5.lan", pattern=r"^https?://[^\s/]+(:\d+)?/?$")
    zertifikatsstelle: Path | None = Field(
        Path("~/Library/Application Support/mkcert/rootCA.pem"),
        description="eigene Zertifikatsstelle für https (leerer Text: die des Systems)")
    merken_s: float = Field(300.0, ge=0, le=3600, description="so lange gilt ein geprüfter App-Schlüssel")
    zeitgrenze_s: float = Field(10.0, gt=0, le=120)

    @field_validator("zertifikatsstelle", mode="before")
    @classmethod
    def _leer_ist_system(cls, wert: object) -> object:
        return None if wert == "" else wert

    @field_serializer("zertifikatsstelle")
    def _system_ist_leer(self, wert: Path | None) -> str:
        return "" if wert is None else str(wert)


KENNUNGEN = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36",
]


class Abruf(_Modell):
    """Wie der Filter weitere Fassungen beim Anbieter holt. Je Kennung eine Fassung: an einem Werbeplatz laufen oft
    mehrere Spots, und ein Spot, der zufällig in beiden Fassungen gleich ist, fällt im Vergleich nicht auf; erst
    mehrere Fassungen zeigen den ganzen Platz (gemessen an Spektrum, 21.11.2026)."""

    kennungen: list[str] = Field(default_factory=lambda: list(KENNUNGEN), min_length=1, max_length=8,
                                 description="User-Agent je weiterer Fassung; Anbieter setzen Werbung je Abruf und Gerät ein")
    zeitgrenze_s: float = Field(600.0, gt=0, le=7200)

    @model_validator(mode="before")
    @classmethod
    def _frueher_eine_kennung(cls, daten: object) -> object:
        """Fassung 0.1: eine Kennung (`kennung`); sie gilt nicht mehr, es gelten die Vorgaben."""
        if isinstance(daten, dict) and "kennung" in daten:
            daten = {k: v for k, v in daten.items() if k != "kennung"}
        return daten


class Grenzen(_Modell):
    groesse_mb: int = Field(800, ge=1, le=10_000, description="größte Fassung, die angenommen oder geholt wird")
    gleichzeitig: int = Field(1, ge=1, le=8, description="so viele Vergleiche rechnen zugleich, die übrigen warten")
    auftraege_behalten_s: int = Field(3600, ge=60, le=7 * 86400, description="so lange bleibt ein fertiger Auftrag abrufbar")


class Klang(_Modell):
    """Schwellen des Vergleichs (das Verfahren selbst steht in klang.py)."""

    gleich: float = Field(0.85, gt=0, lt=1, description="ab dieser Güte gilt ein Stück von 5 s als in der anderen Fassung gefunden")
    stelle_mindestens_s: float = Field(3.0, ge=0.5, le=60, description="kürzere Abschnitte, die nur in einer Fassung vorkommen, zählen nicht")
    luecke_hoechstens_s: float = Field(1.5, ge=0, le=10, description="Stellen mit höchstens so viel Abstand sind ein Werbeplatz")
    gemeinsam_mindestens: float = Field(0.5, gt=0, le=1, description="so viel von A muss in B vorkommen, sonst sind es verschiedene Folgen")


class Signaturen(_Modell):
    """Was als Signatur (Ausschnitt einer eingesetzten Stelle) aufgehoben wird."""

    aufheben: bool = True
    dauer_mindestens_s: float = Field(3.0, ge=0.5, le=600)
    dauer_hoechstens_s: float = Field(300.0, ge=1, le=3600, description="längere Stellen sind eher geänderte Inhalte als Werbung")


class Wiedererkennen(_Modell):
    """Schwellen des Klang-Fingerabdrucks im laufenden Strom (gemessen 23.11.2026: Zufall höchstens 13 Punkte und 2 Prozent,
    echte Treffer im schwächsten Prozent 83 Punkte und 11 Prozent, je Fenster von 4 s)."""

    punkte_verdacht: int = Field(30, ge=10, le=1000, description="zwei solche Fenster nacheinander mit demselben Beginn genügen")
    anteil_verdacht: float = Field(0.04, gt=0, lt=1)
    punkte_sicher: int = Field(60, ge=10, le=1000, description="schon ein Fenster genügt")
    anteil_sicher: float = Field(0.08, gt=0, lt=1)
    ueberall: bool = Field(False, description="bestätigte Signaturen wirken bei jedem Sender; aus: woanders nur als Vorschlag")
    stroeme_hoechstens: int = Field(16, ge=1, le=200)
    stille_s: float = Field(120.0, ge=10, le=3600, description="ein Strom, der so lange nichts schickt, ist beendet")


class Lernlauf(_Modell):
    """Sender im Hintergrund hören und wiederkehrende Stücke sammeln. Diesen Abschnitt stellt auch die App (PUT /api/v1/lernlauf)."""

    an: bool = Field(False, description="im Zeitfenster hören")
    von: str = Field("01:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    bis: str = Field("06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    gleichzeitig: int = Field(8, ge=1, le=12, description="so viele Sender zugleich")
    sitzung_min: int = Field(45, ge=5, le=240, description="so lange am Stück je Sender")
    kbit_hoechstens: int = Field(192, ge=32, le=1500, description="ein Mitschnitt wird nie größer, als er mit dieser Bitrate wäre")
    archiv_h: float = Field(96.0, ge=1, le=1000, description="so lange zurück wird verglichen")
    mindestens_s: float = Field(8.0, ge=3, le=60)
    hoechstens_s: float = Field(65.0, ge=10, le=300)


class Leitstelle(_Modell):
    """Kandidaten mithören lassen (Abschrift) und einschätzen, über die Leitstelle. Ohne Schlüssel ruht das."""

    adresse: str = Field("https://leitstelle.pi5.lan", pattern=r"^https?://[^\s/]+(:\d+)?/?$")
    schluessel: str = ""
    hoerdienst: str = Field("klangwerk-hoeren", max_length=100)
    modell: str = Field("", max_length=200, description="Sprachmodell für die Einschätzung; leer: nur Abschrift")


class Einstellungen(_Modell):
    erlaubte_apps: list[str] = Field(default_factory=lambda: ["radiohub"])
    kellerwolke: KellerwolkeEinstellungen = Field(default_factory=KellerwolkeEinstellungen)
    abruf: Abruf = Field(default_factory=Abruf)
    grenzen: Grenzen = Field(default_factory=Grenzen)
    klang: Klang = Field(default_factory=Klang)
    signaturen: Signaturen = Field(default_factory=Signaturen)
    wiedererkennen: Wiedererkennen = Field(default_factory=Wiedererkennen)
    lernlauf: Lernlauf = Field(default_factory=Lernlauf)
    leitstelle: Leitstelle = Field(default_factory=Leitstelle)

    @model_validator(mode="after")
    def _pruefen(self) -> Einstellungen:
        if not self.erlaubte_apps:
            raise ValueError("Die Erlaubnisliste der Apps ist leer")
        if self.signaturen.dauer_hoechstens_s <= self.signaturen.dauer_mindestens_s:
            raise ValueError("signaturen: dauer_hoechstens_s muss größer als dauer_mindestens_s sein")
        return self

    def app_erlaubt(self, app: str) -> bool:
        return app.casefold() in {a.casefold() for a in self.erlaubte_apps}


class EinstellungsFehler(Exception):
    pass


@dataclass
class _Geladen:
    einstellungen: Einstellungen
    stempel: int


class Einstellungsquelle:
    """Eine Wahrheit für die Einstellungen: die Datei. Gelesen wird neu, sobald sie sich geändert hat."""

    def __init__(self, datei: Path) -> None:
        self.datei = datei
        self._geladen: _Geladen | None = None
        self.fehler: str | None = None
        if not datei.exists():
            self.speichern(Einstellungen())
        self.aktuell()

    def aktuell(self) -> Einstellungen:
        stempel = self.datei.stat().st_mtime_ns
        if self._geladen is None or self._geladen.stempel != stempel:
            try:
                e = Einstellungen.model_validate(tomllib.loads(self.datei.read_text(encoding="utf-8")))
                self._geladen = _Geladen(e, stempel)
                self.fehler = None
            except (ValidationError, tomllib.TOMLDecodeError, ValueError) as f:
                self.fehler = f"{self.datei}: {f}"
                if self._geladen is None:
                    raise EinstellungsFehler(self.fehler) from f
        assert self._geladen is not None
        return self._geladen.einstellungen

    def speichern(self, e: Einstellungen) -> None:
        kopf = ("# Einstellungen des Propaganda-Filters. Gilt ab dem nächsten Auftrag; ungültige Änderungen werden\n"
                "# übergangen (die letzte gültige Fassung gilt weiter, der Stand nennt den Fehler).\n\n")
        neu = self.datei.with_suffix(".toml.neu")
        neu.write_text(kopf + tomli_w.dumps(e.model_dump(mode="json")), encoding="utf-8")
        os.chmod(neu, 0o600)
        neu.replace(self.datei)
