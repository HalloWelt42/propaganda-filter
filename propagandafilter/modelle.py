"""Was die Schnittstelle zeigt (Antworten an die Apps)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .signaturen import Signatur

Zustand = Literal["wartet", "laeuft", "fertig", "fehler"]


class Fundstelle(BaseModel):
    """Ein Abschnitt, der nur in einer Fassung vorkommt (Sekunden dieser Fassung)."""

    von: float
    bis: float
    signatur: str | None = Field(None, description="Kennung der aufgehobenen Signatur, falls eine angelegt wurde")


class Ergebnis(BaseModel):
    gleich: bool = Field(description="beide Fassungen sind Byte für Byte gleich (dann ist nichts eingesetzt)")
    verwertbar: bool = Field(description="die Fassungen sind dieselbe Folge (genug gemeinsamer Klang)")
    dauer_a: float | None = None
    dauer_b: float | None = None
    anteil_gemeinsam: float | None = Field(None, description="Anteil von A, der auch in B vorkommt")
    nur_in_a: list[Fundstelle] = Field(default_factory=list, description="nur in der eingereichten Fassung")
    nur_in_b: list[Fundstelle] = Field(default_factory=list, description="nur in der vom Filter geholten Fassung")
    hinweis: str | None = None


class Auftrag(BaseModel):
    id: str
    app: str
    quelle: str
    zustand: Zustand = "wartet"
    schritt: str = "wartet auf einen freien Platz"
    fortschritt: float = Field(0.0, ge=0, le=1)
    angelegt: str
    beendet: str | None = None
    fehler: str | None = None
    ergebnis: Ergebnis | None = None


class Stand(BaseModel):
    dienst: str
    fassung: str
    app: str
    einstellungen_fehler: str | None
    auftraege: dict[Zustand, int]
    signaturen: dict[str, int]


class Signaturliste(BaseModel):
    signaturen: list[Signatur]


class Beurteilung(BaseModel):
    urteil: Literal["offen", "bestaetigt", "verworfen"]
