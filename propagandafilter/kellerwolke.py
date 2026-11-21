"""Prüfung bei der Kellerwolke: wer ist die App hinter einem App-Schlüssel (wie im Befehlsdienst).

Der Filter fragt selbst nach; was eine App über sich behauptet, zählt nicht. Geprüfte App-Schlüssel gelten eine Weile
(Einstellung `merken_s`).
"""

from __future__ import annotations

import hashlib
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from .einstellungen import KellerwolkeEinstellungen


class NichtAngemeldet(Exception):
    """Schlüssel oder Sitzung fehlen, sind unbekannt oder abgelaufen."""


class NichtErreichbar(Exception):
    """Die Kellerwolke antwortet nicht oder nicht verständlich."""


class _Antwort(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AppIdentitaet(_Antwort):
    """Antwort von GET /api/v1/ablage/ich (nur was der Dienst braucht)."""

    app: str
    konto: str
    konto_id: UUID


class KellerwolkePruefer:
    def __init__(self, einstellungen: Callable[[], KellerwolkeEinstellungen], *,
                 transport: httpx.AsyncBaseTransport | None = None, uhr: Callable[[], float] = time.monotonic) -> None:
        self._einstellungen = einstellungen
        self._transport = transport
        self._uhr = uhr
        self._client: httpx.AsyncClient | None = None
        self._client_fuer: KellerwolkeEinstellungen | None = None
        self._abgeloest: list[httpx.AsyncClient] = []
        self._gemerkt: dict[str, tuple[AppIdentitaet, float]] = {}

    async def schliessen(self) -> None:
        for client in [*self._abgeloest, *([self._client] if self._client else [])]:
            await client.aclose()
        self._abgeloest.clear()
        self._client = None

    async def app(self, schluessel: str) -> AppIdentitaet:
        if not schluessel:
            raise NichtAngemeldet("App-Schlüssel fehlt")
        e = self._einstellungen()
        merkmal = hashlib.sha256(schluessel.encode()).hexdigest()
        gemerkt = self._gemerkt.get(merkmal)
        if gemerkt and gemerkt[1] > self._uhr():
            return gemerkt[0]
        antwort = await self._get("/api/v1/ablage/ich", {"Authorization": f"Bearer {schluessel}"})
        if antwort.status_code in (401, 403):
            self._gemerkt.pop(merkmal, None)
            raise NichtAngemeldet("App-Schlüssel ist der Kellerwolke unbekannt oder gesperrt")
        ich = self._lesen(antwort, AppIdentitaet)
        self._gemerkt = {k: v for k, v in self._gemerkt.items() if v[1] > self._uhr()}
        self._gemerkt[merkmal] = (ich, self._uhr() + e.merken_s)
        return ich

    # --- intern ---------------------------------------------------------------------------------------------------

    async def _get(self, pfad: str, kopf: dict[str, str]) -> httpx.Response:
        e = self._einstellungen()
        try:
            return await self._verbindung(e).get(e.adresse.rstrip("/") + pfad, headers=kopf)
        except httpx.HTTPError as f:
            raise NichtErreichbar(f"Kellerwolke nicht erreichbar: {type(f).__name__}") from f

    @staticmethod
    def _lesen[T: BaseModel](antwort: httpx.Response, modell: type[T]) -> T:
        if antwort.status_code != 200:
            raise NichtErreichbar(f"Kellerwolke antwortet mit {antwort.status_code}")
        try:
            return modell.model_validate_json(antwort.content)
        except ValidationError as f:
            raise NichtErreichbar("Antwort der Kellerwolke ist nicht lesbar") from f

    def _verbindung(self, e: KellerwolkeEinstellungen) -> httpx.AsyncClient:
        """Eine Verbindung je Einstellung; ändern sich Adresse oder Zertifikatsstelle, entsteht eine neue."""
        if self._client is None or self._client_fuer != e:
            if self._client is not None:
                self._abgeloest.append(self._client)  # laufende Anfragen dürfen zu Ende laufen
            self._client = httpx.AsyncClient(timeout=e.zeitgrenze_s, verify=_pruefung(e.zertifikatsstelle),
                                             transport=self._transport)
            self._client_fuer = e
            self._gemerkt.clear()
        return self._client


def _pruefung(stelle: Path | None) -> ssl.SSLContext | bool:
    if stelle is None:
        return True
    datei = stelle.expanduser()
    if not datei.is_file():
        raise NichtErreichbar(f"Zertifikatsstelle {datei} fehlt")
    return ssl.create_default_context(cafile=str(datei))
