"""Die Schnittstelle von außen: wer fragen darf, ein Vergleich als Auftrag, Signaturen anhören und beurteilen."""

from conftest import FOLGE, FUNDUS, RADIOHUB, warten


async def _bestellen(c, datei, quelle="Probe: Folge 1"):
    r = await c.post("/api/v1/vergleiche", params={"zweite": FOLGE, "quelle": quelle}, content=datei.read_bytes(),
                     headers={**RADIOHUB, "content-type": "application/octet-stream"})
    assert r.status_code == 202, r.text
    return r.json()


async def test_ohne_schluessel_und_fremde_app(filter_):
    assert (await filter_.get("/api/v1/stand")).status_code == 401
    assert (await filter_.get("/api/v1/stand", headers={"Authorization": "Bearer falsch"})).status_code == 401
    assert (await filter_.get("/api/v1/stand", headers=FUNDUS)).status_code == 403
    stand = (await filter_.get("/api/v1/stand", headers=RADIOHUB)).json()
    assert stand["app"] == "RadioHub" and stand["signaturen"]["offen"] == 0


async def test_vergleich_findet_spot_und_hebt_signatur_auf(filter_, anbieter, fassungen):
    anbieter.datei = fassungen["b"]
    auftrag = await _bestellen(filter_, fassungen["a"])
    assert auftrag["zustand"] in ("wartet", "laeuft")
    fertig = await warten(filter_, auftrag["id"])
    assert fertig["zustand"] == "fertig", fertig
    e = fertig["ergebnis"]
    assert not e["gleich"] and e["verwertbar"] and len(e["abgleiche"]) == 3
    assert all(x["nur_in_b"] == [] and not x["gleich"] for x in e["abgleiche"])
    assert len(set(anbieter.kennungen)) == 3                    # je Fassung eine andere Kennung
    [stelle] = e["nur_in_a"]
    assert abs(stelle["von"] - 20) <= 0.1 and abs(stelle["bis"] - 28) <= 0.1 and stelle["signatur"]

    liste = (await filter_.get("/api/v1/signaturen", headers=RADIOHUB)).json()["signaturen"]
    [sig] = liste
    assert sig["id"] == stelle["signatur"] and sig["quelle"] == "Probe: Folge 1" and sig["app"] == "RadioHub"
    assert sig["fassung"] == "a" and abs(sig["dauer_s"] - 8) <= 0.2 and sig["urteil"] == "offen"
    ton = await filter_.get(f"/api/v1/signaturen/{sig['id']}/ton", headers=RADIOHUB)
    assert ton.status_code == 200 and ton.headers["content-type"] == "audio/mp4" and len(ton.content) > 10_000

    r = await filter_.put(f"/api/v1/signaturen/{sig['id']}", json={"urteil": "verworfen"}, headers=RADIOHUB)
    assert r.json()["urteil"] == "verworfen" and r.json()["beurteilt"]
    assert (await filter_.get("/api/v1/signaturen", params={"urteil": "offen"}, headers=RADIOHUB)).json()["signaturen"] == []
    # Aufträge sieht nur die App, die sie bestellt hat
    assert (await filter_.get(f"/api/v1/vergleiche/{auftrag['id']}", headers=FUNDUS)).status_code == 403


async def test_werbeplatz_aus_mehreren_fassungen(filter_, anbieter, fassungen):
    """Spektrum 21.11.2026: ein Platz aus zwei Spots; je Abruf ist zufällig einer davon gleich. Erst alle Fassungen
    zusammen zeigen den ganzen Platz, als eine Stelle."""
    anbieter.dateien = [fassungen["nur_x"], fassungen["nur_y"], fassungen["platz"]]
    fertig = await warten(filter_, (await _bestellen(filter_, fassungen["platz"]))["id"])
    e = fertig["ergebnis"]
    einzeln = [[(round(x["von"]), round(x["bis"])) for x in g["nur_in_a"]] for g in e["abgleiche"]]
    assert einzeln == [[(30, 40)], [(20, 30)], []]                 # die dritte Fassung ist byte-gleich
    assert e["abgleiche"][2]["gleich"] and not e["gleich"]
    [platz] = e["nur_in_a"]
    assert abs(platz["von"] - 20) <= 0.1 and abs(platz["bis"] - 40) <= 0.1 and platz["signatur"]


async def test_gleiche_bytes(filter_, anbieter, fassungen):
    anbieter.datei = fassungen["b"]
    fertig = await warten(filter_, (await _bestellen(filter_, fassungen["b"]))["id"])
    assert fertig["ergebnis"]["gleich"] and fertig["ergebnis"]["nur_in_a"] == []


async def test_andere_folge_nennt_keine_stellen(filter_, anbieter, fassungen):
    anbieter.datei = fassungen["anders"]
    fertig = await warten(filter_, (await _bestellen(filter_, fassungen["b"]))["id"])
    e = fertig["ergebnis"]
    assert not e["verwertbar"] and e["nur_in_a"] == [] and "dieselbe Folge" in e["hinweis"]
    assert (await filter_.get("/api/v1/stand", headers=RADIOHUB)).json()["signaturen"]["offen"] == 0


async def test_anbieter_liefert_nicht(filter_, anbieter, fassungen):
    anbieter.status = 404
    fertig = await warten(filter_, (await _bestellen(filter_, fassungen["a"]))["id"])
    assert fertig["zustand"] == "fehler" and "404" in fertig["fehler"]


async def test_falsche_anfragen(filter_, fassungen):
    r = await filter_.post("/api/v1/vergleiche", params={"zweite": "ftp://x"}, content=b"x", headers=RADIOHUB)
    assert r.status_code == 422
    r = await filter_.post("/api/v1/vergleiche", params={"zweite": FOLGE}, content=b"", headers=RADIOHUB)
    assert r.status_code == 400
    assert (await filter_.get("/api/v1/vergleiche/00000000-0000-4000-8000-000000000000", headers=RADIOHUB)).status_code == 404
