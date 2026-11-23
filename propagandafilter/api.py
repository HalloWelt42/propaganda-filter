"""HTTP-Schnittstelle /api/v1 für die Apps (Vertrag in README.md, Schema unter /openapi.json). Übersetzt nur; wer
fragen darf, entscheiden Kellerwolke und Erlaubnisliste, was gerechnet wird, Vergleicher und Auftragsbuch."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .abschrift import Abschrift
from .auftraege import Auftragsbuch
from .einstellungen import Einstellungsquelle
from .fassung import fassung
from .klang import KlangFehler
from .kellerwolke import AppIdentitaet, KellerwolkePruefer, NichtAngemeldet, NichtErreichbar
from .lernbuch import Lernsender, Senderseite, Senderstand
from .lernlauf import Lernlauf, Lernstand
from .modelle import (
    Auftrag, Beurteilung, Fachseite, Lernlaufstellung, NeuerStrom, Quellenstand, Senderausnahme, Signaturliste, Sofortlauf, Spotstelle, Stand, Stromstand,
)
from .signaturen import ENDUNG, Fach, Signatur, SignaturFehler, Signaturbuch, Urteil
from .strom import Fund, Strom, StromFehler, Stroeme
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
                  vergleicher: Vergleicher, signaturen: Signaturbuch, stroeme: Stroeme, lernlauf: Lernlauf,
                  abschrift: Abschrift) -> APIRouter:
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
                     auftraege=auftraege.zahlen(), signaturen=signaturen.zahlen(), stroeme=len(stroeme.alle()),
                     abschrift=abschrift.eingerichtet, abschrift_fehler=abschrift.fehler)

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

    def gefunden(strom: Strom, fund: Fund) -> None:
        signaturen.treffer(fund.spot, strom.quelle, strom.name)

    def _stand(s: Strom) -> Stromstand:
        ueberall = quelle.aktuell().wiedererkennen.ueberall
        stellen = [Spotstelle(signatur=f.spot, von=round(f.von, 2), bis=round(f.bis, 2), sicher_ab=round(f.sicher_ab, 2),
                              wirkt=signaturen.wirkt(f.spot, s.quelle, ueberall), werbender=signaturen.eine(f.spot).werbender,
                              abgebrochen=f.abgebrochen) for f in s.funde]
        return Stromstand(id=s.id, quelle=s.quelle, geprueft_bis=round(s.geprueft_bis, 2), stellen=stellen)

    def _strom(kennung: UUID, app: AppIdentitaet) -> Strom:
        try:
            return stroeme.holen(str(kennung), app.app)
        except KeyError:
            raise HTTPException(404, "Strom unbekannt oder beendet") from None

    @router.post("/stroeme", response_model=Stromstand, status_code=201)
    async def strom_anlegen(neu: NeuerStrom, app: App) -> Stromstand:
        """Einen laufenden Strom auf bekannte Spots prüfen; der Ton folgt in Stücken unter /stroeme/{id}/ton."""
        s = Strom(app=app.app, quelle=neu.quelle, name=neu.name, art=neu.art, einstellungen=lambda: quelle.aktuell().wiedererkennen,
                  verzeichnis=signaturen.verzeichnis, bei_fund=gefunden)
        try:
            return _stand(await stroeme.anlegen(s))
        except StromFehler as f:
            raise HTTPException(503, str(f)) from f

    @router.post("/stroeme/{strom_id}/ton", response_model=Stromstand,
                 openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {
                     "schema": {"type": "string", "format": "binary"}}}}})
    async def strom_ton(strom_id: UUID, request: Request, app: App, ab: Annotated[float, Query(ge=0)]) -> Stromstand:
        """Das nächste Stück Ton, lückenlos, so wie es vom Sender kommt; `ab` zählt nur beim ersten. Antwort: bis wohin geprüft
        ist und welche Spots der Strom enthält."""
        s = _strom(strom_id, app)
        try:
            await s.nehmen(await request.body(), ab)
        except StromFehler as f:
            await stroeme.beenden(s.id)
            raise HTTPException(422, str(f)) from f
        return _stand(s)

    @router.get("/stroeme/{strom_id}", response_model=Stromstand)
    async def strom_stand(strom_id: UUID, app: App) -> Stromstand:
        return _stand(_strom(strom_id, app))

    @router.delete("/stroeme/{strom_id}", status_code=204)
    async def strom_beenden(strom_id: UUID, app: App) -> None:
        await stroeme.beenden(_strom(strom_id, app).id)

    @router.post("/signaturen", response_model=Signatur, status_code=201,
                 responses={200: {"model": Signatur, "description": "gab es schon (gleicher Klang)"}},
                 openapi_extra={"requestBody": {"required": True, "content": {"application/octet-stream": {
                     "schema": {"type": "string", "format": "binary"}}}}})
    async def signatur_anlegen(request: Request, app: App, antwort: Response,
                               quelle_kennung: Annotated[str, Query(alias="quelle", min_length=1, max_length=200)],
                               name: Annotated[str, Query(max_length=300)],
                               von: Annotated[float, Query(ge=0)], bis: Annotated[float, Query(gt=0)],
                               herkunft: Literal["angabe", "schnitt"], werbender: Annotated[str, Query(max_length=200)] = "") -> Signatur:
        """Bekannte Werbung als Signatur: angekündigt (Sendedaten) oder im Schnitt markiert. Körper ist der Ton, [von, bis) die
        Werbung darin in Sekunden. Gibt es denselben Klang schon, kommt nur die Quelle dazu."""
        e = quelle.aktuell()
        if not e.signaturen.dauer_mindestens_s <= bis - von <= e.signaturen.dauer_hoechstens_s:
            raise HTTPException(422, f"Werbung muss {e.signaturen.dauer_mindestens_s:g} bis {e.signaturen.dauer_hoechstens_s:g} Sekunden lang sein")
        kennung = str(uuid4())
        ordner = vergleicher.ordner(kennung)
        try:
            ton = ordner / "ton"
            await _empfangen(request, ton, e.grenzen.groesse_mb * 1024 * 1024)
            vorhanden = await asyncio.to_thread(signaturen.gleiche, ton, (von, bis), e.wiedererkennen)
            if vorhanden:
                signaturen.treffer(vorhanden, quelle_kennung, name)
                if signaturen.eine(vorhanden).urteil != "verworfen":
                    signaturen.quelle_setzen(vorhanden, quelle_kennung, "wirkt")      # hier angekündigt, also hier Werbung
                antwort.status_code = 200
                return signaturen.eine(vorhanden)
            return await asyncio.to_thread(signaturen.aufnehmen, ton, von, bis, quelle=name, app=app.app, herkunft=herkunft,
                                           fassung="", auftrag="", werbender=werbender, quelle_kennung=quelle_kennung)
        except (SignaturFehler, KlangFehler) as f:
            raise HTTPException(422, str(f)) from f
        finally:
            vergleicher.wegraeumen(kennung)

    @router.put("/signaturen/{signatur_id}/quellen/{quelle_kennung}", response_model=Signatur)
    async def quelle_setzen(signatur_id: UUID, quelle_kennung: str, neu: Quellenstand, _app: App) -> Signatur:
        """Einen Vorschlag für eine Quelle annehmen (wirkt) oder zurücknehmen (vorschlag)."""
        try:
            return signaturen.quelle_setzen(_signatur(signatur_id), quelle_kennung, neu.stand)
        except KeyError:
            raise HTTPException(404, "Quelle unbekannt") from None

    @router.get("/signaturen", response_model=Signaturliste)
    async def liste(_app: App, urteil: Urteil | None = None,
                    grenze: Annotated[int, Query(ge=1, le=1000)] = 200) -> Signaturliste:
        return Signaturliste(signaturen=signaturen.liste(urteil, grenze))

    @router.get("/signaturen/fach/{fach}", response_model=Fachseite)
    async def fachseite(fach: Fach, _app: App, ab: Annotated[int, Query(ge=0)] = 0,
                        anzahl: Annotated[int, Query(ge=1, le=200)] = 25) -> Fachseite:
        """Durchsicht: ein Stück eines Fachs."""
        return Fachseite(faecher=signaturen.faecher(), signaturen=signaturen.im_fach(fach, ab, anzahl))

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

    # ----- Lernlauf

    @router.get("/lernlauf", response_model=Lernstand)
    async def lernstand(_app: App) -> Lernstand:
        """Was der Lernlauf gerade hört, als Nächstes hört und in den letzten Läufen fand."""
        return lernlauf.stand()

    @router.put("/lernlauf", response_model=Lernstand)
    async def lernlauf_stellen(neu: Lernlaufstellung, _app: App) -> Lernstand:
        e = quelle.aktuell()
        quelle.speichern(e.model_copy(update={"lernlauf": e.lernlauf.model_copy(update=neu.model_dump(exclude_none=True))}))
        return lernlauf.stand()

    @router.post("/lernlauf/jetzt", response_model=Lernstand)
    async def lernlauf_jetzt(neu: Sofortlauf, _app: App) -> Lernstand:
        """Auch außerhalb des Zeitfensters hören."""
        lernlauf.jetzt_laufen(neu.minuten)
        return lernlauf.stand()

    @router.get("/lernlauf/sender", response_model=Senderseite)
    async def lernsender(_app: App, ab: Annotated[int, Query(ge=0)] = 0, anzahl: Annotated[int, Query(ge=1, le=200)] = 25) -> Senderseite:
        return lernlauf.buch.sender(ab, anzahl)

    @router.put("/lernlauf/sender", response_model=Lernstand)
    async def lernsender_setzen(sender: Annotated[list[Lernsender], Body(max_length=2000)], _app: App) -> Lernstand:
        """Die ganze Liste der App, mit Grund und Gewicht; was fehlt, ruht."""
        lernlauf.buch.sender_setzen(sender)
        return lernlauf.stand()

    @router.put("/lernlauf/sender/{quelle_kennung}", response_model=Senderstand)
    async def lernsender_ausnehmen(quelle_kennung: str, neu: Senderausnahme, _app: App) -> Senderstand:
        try:
            lernlauf.buch.ausnehmen(quelle_kennung, neu.aus)
        except KeyError:
            raise HTTPException(404, "Sender unbekannt") from None
        return lernlauf.buch.einer(quelle_kennung)

    return router

