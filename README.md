# Propaganda-Filter

Kleiner Dienst am Mac, der Werbung in Ton findet, für eigene Apps (zuerst RadioHub). Die Apps liefern nur Daten, der
Mac rechnet. Gefundene Stellen hebt der Filter als Signaturen auf: kurze Ausschnitte, die ein Mensch anhören,
bestätigen oder verwerfen kann. Die Signaturen liegen im Datenordner, nie im Repository.

Erste Funktion: **Fassungen derselben Folge vergleichen.** Viele Anbieter setzen Werbung erst beim Abruf ein, je
Abruf andere. Die App reicht ihre gespeicherte Datei ein (Fassung A), der Filter holt dieselbe Folge selbst mehrmals
(je Kennung eine weitere Fassung), richtet jede am Klang an A aus und nennt auf 10 ms genau, was in A vorkommt, in einer
anderen aber fehlt. An einem Werbeplatz laufen oft mehrere Spots; einer davon kann in einer weiteren Fassung zufällig
gleich sein und fällt erst gegen eine andere auf. Darum zählen alle Vergleiche zusammen, und Spots mit höchstens
`klang.luecke_hoechstens_s` Abstand bilden einen Werbeplatz.

## Vertrag

Schema unter `https://<mac>:<vorne>/openapi.json` (über den Vorbau des Macs). Jede Anfrage trägt
`Authorization: Bearer <App-Schlüssel der Kellerwolke>`; der Filter fragt die Kellerwolke selbst
(`GET /api/v1/ablage/ich`, gemerkt 5 Minuten) und lässt nur Apps der Erlaubnisliste zu. Ohne gültigen Schlüssel kommt
401, eine App außerhalb der Liste bekommt 403, eine nicht erreichbare Kellerwolke 503.

| Aufruf | Körper | Antwort |
|---|---|---|
| `GET /api/v1/stand` | | `{dienst, fassung, app, einstellungen_fehler, auftraege, signaturen}` |
| `POST /api/v1/vergleiche?zweite=<Adresse>&quelle=<Text>` | Fassung A als Bytes (`application/octet-stream`) | 202, Auftrag |
| `GET /api/v1/vergleiche/{id}` | | Auftrag (nur für die App, die ihn bestellt hat) |
| `GET /api/v1/signaturen?urteil=offen\|bestaetigt\|verworfen&grenze=200` | | `{signaturen: [...]}`, neueste zuerst |
| `GET /api/v1/signaturen/{id}/ton` | | Ausschnitt als `audio/mp4` |
| `PUT /api/v1/signaturen/{id}` | `{urteil}` | Signatur mit Urteil |

**Auftrag:** `id, app, quelle, zustand` (`wartet`, `laeuft`, `fertig`, `fehler`), `schritt` (was gerade geschieht),
`fortschritt` (0 bis 1), `angelegt, beendet, fehler, ergebnis`. Es rechnen höchstens `grenzen.gleichzeitig` Aufträge
zugleich; fertige bleiben `grenzen.auftraege_behalten_s` abrufbar und überleben keinen Neustart.

**Ergebnis:** `gleich` (alle Fassungen Byte für Byte gleich mit A, dann ist nichts eingesetzt), `verwertbar` (mindestens
eine Fassung hat genug gemeinsamen Klang, sonst sind es verschiedene Folgen und es werden keine Stellen genannt),
`dauer_a`, `anteil_gemeinsam` (der kleinste), `nur_in_a` als `{von, bis, signatur}`: die Werbeplätze in A, vereinigt
über alle Vergleiche; `abgleiche` je weitere Fassung `{kennung, gleich, verwertbar, dauer, anteil_gemeinsam, nur_in_a,
nur_in_b}` (Sekunden der jeweiligen Fassung), `hinweis`. Die Zeiten von `nur_in_a` gelten genau für die eingereichte
Datei.

**Signatur:** `id, quelle, app, herkunft` (`vergleich`), `fassung` (`a` oder `b`), `von_s, bis_s, dauer_s, auftrag,
angelegt, urteil, beurteilt`. Verworfene bleiben mit ihrem Urteil stehen; gelöscht wird nichts.

## Verfahren

Dekodieren (mono, 8 kHz), je 10 ms die Energie in 24 Bändern (log), Fassung A in Stücke von 5 s teilen und jedes Stück
in B per Korrelation suchen (über FFT, erst am erwarteten Versatz, sonst über die ganze Fassung). Die gemeinsamen Läufe
werden danach auf die Zeile genau begrenzt; dazwischen liegt, was nur in A vorkommt. Beide Richtungen werden gerechnet.
Das Verfahren steht in `propagandafilter/klang.py`, die Schwellen in den Einstellungen (`[klang]`).

## Einrichten am Mac

```bash
tools/einrichten.sh
```

Legt das venv an (Python 3.13), installiert die Pakete, setzt die Git-Hooks und trägt den Dienst im Stellwerk ein
(lokal `127.0.0.1:8572`). Von außen nur über den Vorbau des Macs (HTTPS, Port 8573). Braucht FFmpeg im Suchpfad.

Datenordner: `~/Library/Application Support/Propaganda-Filter` (oder `PROPAGANDAFILTER_DATEN`) mit
`einstellungen.toml` (Vorgaben beim ersten Start; Erlaubnisliste, Kellerwolke, Abruf mit den Kennungen je weiterer
Fassung, Grenzen, Schwellen, Signaturen), `signaturen/` (Datenbank und Ausschnitte) und `arbeit/` (Dateien laufender Vergleiche, danach entfernt).
Geänderte Einstellungen gelten ab dem nächsten Auftrag; eine ungültige Fassung wird übergangen, `GET /api/v1/stand`
nennt den Fehler.

Prüfen:

```bash
.venv/bin/python -m pytest
```

## Lizenz

**Nicht-kommerzielle Nutzung** - Siehe [LICENSE](LICENSE)

Erlaubt: Private Nutzung, Installation, persönliche Anpassungen, Teilen mit Quellenangabe

Verboten: Kommerzielle Nutzung, Verkauf, Einbindung in kommerzielle Produkte

---

## Unterstützen

Propaganda-Filter ist ein privates Hobby-Projekt. Kein Tracking, keine Werbung, keine Kompromisse.

Wenn dir das Projekt gefällt, kannst du es weiterempfehlen und Werbung gemeinsam leiser machen - oder direkt hier:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/HalloWelt42)

**Crypto:**

| Coin | Adresse |
|------|---------|
| BTC | `bc1qnd599khdkv3v3npmj9ufxzf6h4fzanny2acwqr` |
| DOGE | `DL7tuiYCqm3xQjMDXChdxeQxqUGMACn1ZV` |
| ETH | `0x8A28fc47bFFFA03C8f685fa0836E2dBe1CA14F27` |

Copyright (c) 2025-2026 HalloWelt42
