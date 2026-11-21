"""HTTP-Schnittstelle /api/v1 für die Apps (Vertrag in README.md, Schema unter /openapi.json). Übersetzt nur; wer
fragen darf, entscheiden Kellerwolke und Erlaubnisliste, was gerechnet wird, Vergleicher und Auftragsbuch."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auftraege import Auftragsbuch
from .einstellungen import Einstellungsquelle
from .fassung import fassung
from .kellerwolke import AppIdentitaet, KellerwolkePruefer, NichtAngemeldet, NichtErreichbar
from .modelle import Auftrag, Beurteilung, Signaturliste, Stand
from .signaturen import ENDUNG, Signatur, Signaturbuch, Urteil
from .vergleich import Vergleicher

_bearer = HTTPBearer(auto_error=False, description="App-Schlüssel der Kellerwolke")


def fehler_einhaengen(app: FastAPI) -> None:
    def antwort(status: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handler(_r: Request, f: Exception) -> JSONResponse:
            kopf = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            return JSONResponse({"detail": str(f)}, status_code=status, headers=kopf)
        return handler

    for art, status in ((NichtAngemeldet, 401), (NichtErreichbar, 503)):
        app.add_exception_handler(art, antwort(status))


async def _empfangen(request: Request, ziel: Path, grenze: int) -> int:
    """Den Körper der Anfrage (Fassung A) in eine Datei schreiben, ohne ihn ganz im Speicher zu halten."""
    groesse = 0
    with ziel.open("wb") as f:
        async for teil in request.stream():
            groesse += len(teil)
            if groesse > grenze:
                raise HTTPException(413, f"Fassung größer als {grenze // (1024 * 1024)} MB")
            f.write(teil)
    if groesse == 0:
        raise HTTPException(400, "Fassung A fehlt (Körper der Anfrage ist leer)")
    return groesse


def schnittstelle(quelle: Einstellungsquelle, pruefer: KellerwolkePruefer, auftraege: Auftragsbuch,
                  vergleicher: Vergleicher, signaturen: Signaturbuch) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["filter"])

    async def aktuelle_app(anmeldung: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> AppIdentitaet:
        ich = await pruefer.app(anmeldung.credentials if anmeldung else "")
        if not quelle.aktuell().app_erlaubt(ich.app):
            raise HTTPException(403, f"Die App {ich.app} steht nicht auf der Erlaubnisliste des Filters")
        return ich

    App = Annotated[AppIdentitaet, Depends(aktuelle_app)]

    @router.get("/stand", response_model=Stand)
    async def stand(app: App) -> Stand:
        quelle.aktuell()
        return Stand(dienst="Propaganda-Filter", fassung=fassung(), app=app.app, einstellungen_fehler=quelle.fehler,
                     auftraege=auftraege.zahlen(), signaturen=signaturen.zahlen())

    @router.post("/vergleiche", response_model=Auftrag, status_code=202,
                 openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {
                     "schema": {"type": "string", "format": "binary"}}}}},
                 responses={413: {"description": "Fassung zu groß"}})
    async def vergleichen(request: Request, app: App,
                          zweite: Annotated[str, Query(pattern=r"^https?://\S+$", max_length=4000,
                                                       description="Adresse der Folge beim Anbieter (Fassung B)")],
                          quelle_text: Annotated[str, Query(alias="quelle", max_length=300,
                                                            description="was die Folge ist, für die Signaturen")] = "") -> Auftrag:
        """Fassung A als Körper (die gespeicherte Datei der App, nur für sie gelten die Zeiten); Fassung B holt der
        Filter von `zweite`. Antwort sofort mit dem Auftrag; den Stand fragt die App unter /vergleiche/{id} ab."""
        e = quelle.aktuell()
        kennung = str(uuid4())
        try:
            await _empfangen(request, vergleicher.ordner(kennung) / "a", e.grenzen.groesse_mb * 1024 * 1024)
        except BaseException:
            vergleicher.wegraeumen(kennung)
            raise
        # erst mit vollständiger Fassung A wird ein Auftrag daraus
        return auftraege.anlegen(app.app, quelle_text, lambda a: vergleicher.ausfuehren(a, zweite), kennung=kennung)

    @router.get("/vergleiche/{auftrag_id}", response_model=Auftrag)
    async def vergleich(auftrag_id: UUID, app: App) -> Auftrag:
        try:
            return auftraege.holen(str(auftrag_id), app.app)
        except KeyError:
            raise HTTPException(404, "Auftrag unbekannt oder abgelaufen") from None

    @router.get("/signaturen", response_model=Signaturliste)
    async def liste(_app: App, urteil: Urteil | None = None,
                    grenze: Annotated[int, Query(ge=1, le=1000)] = 200) -> Signaturliste:
        return Signaturliste(signaturen=signaturen.liste(urteil, grenze))

    def _signatur(kennung: UUID) -> str:
        try:
            signaturen.eine(str(kennung))
        except KeyError:
            raise HTTPException(404, "Signatur unbekannt") from None
        return str(kennung)

    @router.get("/signaturen/{signatur_id}/ton", response_class=FileResponse)
    async def ton(signatur_id: UUID, _app: App) -> FileResponse:
        kennung = _signatur(signatur_id)
        return FileResponse(signaturen.tondatei(kennung), media_type="audio/mp4", filename=f"{kennung}{ENDUNG}")

    @router.put("/signaturen/{signatur_id}", response_model=Signatur)
    async def beurteilen(signatur_id: UUID, b: Beurteilung, _app: App) -> Signatur:
        """Bestätigen oder verwerfen (Irrtümer herausfiltern); verworfene bleiben mit ihrem Urteil stehen."""
        return signaturen.beurteilen(_signatur(signatur_id), b.urteil)

    return router

