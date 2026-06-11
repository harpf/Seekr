# Scan-Eingang (Hot-Folder-Ingestion mit Review-Warteschlange) — Design

**Datum:** 2026-06-11
**Status:** Approved (Brainstorming abgeschlossen, bereit für Implementierungsplan)

## Ziel

Netzwerk-Scanner legen Dokumente in einen überwachten Netzwerkordner ab. Seekr arbeitet
diese Scans nach und nach ab: OCR, Indexierung, KI-Vorschlag für die Ablage in der
bestehenden Ordnerstruktur — und stellt sie nach menschlicher Bestätigung in der richtigen
Struktur bereit. Die Ablage erfolgt **nicht** vollautomatisch, sondern über eine
**Review-Warteschlange**, in der ein Zuständiger den Vorschlag bestätigt oder korrigiert.

## Kernentscheidungen (aus dem Brainstorming)

| Thema | Entscheidung |
|---|---|
| Ablage-Modell | **Review-Warteschlange** — OCR + Index + KI-Vorschlag, dann menschliche Bestätigung vor dem Verschieben |
| Trigger | **Filesystem-Events (watchdog) + Polling-Fallback** pro Eingang |
| Datei-Lifecycle | Stabile Datei wird ins **Staging** (`pending-review/<inbox>/`) verschoben; Original bleibt erhalten bis zur Bestätigung |
| Stabilitätsfenster | **300 s (5 Min)** — Datei gilt als fertig, wenn Größe **und** mtime unverändert bleiben |
| Zuständigkeit | **Admins immer**; zusätzlich **Rollen + einzelne Benutzer** pro Eingang zuweisbar |
| Anzahl Eingänge | **Mehrere benannte Eingänge**, jeder mit eigener Ziel-Wurzel |
| Dateiformate | **PDF + Bildformate** (`.jpg/.png/.tiff`) → neuer Bild-Extraktor; OCR im Scan-Pfad **immer aktiv** |
| Ziel-Wurzel | **Pro Eingang konfigurierbar** |
| Zielordner-Wahl | **Eingeschränkt auf existierende Unterordner** der Ziel-Wurzel (KI wählt nur daraus; „neuer Ordner" als bewusste Aktion) |
| Konfiguration | **Über die Admin-UI** (Config → Scan-Eingänge); `config.json` ist nur Persistenz |
| Struktur-Verbesserung | **Out of Scope** — das bestehende Feature „AI: Folder Structure Suggestions" deckt das ab |

## Gewählter Ansatz

**Ansatz A — Eigene Scan-Pipeline auf dem bestehenden Job-System.** Der Scan-Eingang ist
ein eigener Ingestion-Weg *parallel* zum manuellen Upload. Er teilt sich Extraktoren, OCR,
Index und die „Apply & move file"-Bewegung, kapselt Watcher- und Review-Zustand aber sauber.
Verworfen: (B) Upload-Flow generalisieren — vermischt Verantwortlichkeiten; (C) externes Tool
(Paperless-ngx) — zweites System, kein integriertes Review, widerspricht „lokal & integriert".

## Architektur & Komponenten

Entlang der bestehenden Pipeline (Crawler → Extractors → Services → Index → Web):

| Baustein | Art | Verantwortung |
|---|---|---|
| `services/scan_watcher.py` | neu | Pro Eingang ein Watcher: watchdog-Events **+ Polling-Fallback**. Stabilitätserkennung (5 Min), Move ins Staging, reiht `scan_ingest`-Job ein. Kapselt Stabilitäts- und Claim-Logik. |
| `extractors/image_extractor.py` | neu | `.jpg/.png/.tiff` → `ocr_service` (Tesseract) → `ExtractionResult` mit einem `page`-Content-Block. Fügt sich in die `extractors/`-Registry ein. |
| `scan_ingest` Worker-Handler | neu (in `app.py`, analog `index_paths`) | OCR-erzwungene Extraktion + Index + validierter KI-Vorschlag → `scan_review`-Eintrag. |
| `services/scan_review_store.py` | neu | Einziger Schreibpfad auf `scan_review` (CRUD, Statusübergänge, ACL-Vergabe). |
| Review-UI + API-Routen | neu | „Scan-Posteingang" unter *Ingest*; CRUD-UI „Scan-Eingänge" unter *Config*. |
| `config.py` (`scan_inboxes`) | erweitert | Liste benannter Eingänge. |

### End-to-End-Datenfluss

```
Drucker → SMB-Netzwerkordner (Eingang)
   │  scan_watcher: Event/Poll → Stabilitäts-Check (5 Min: Größe & mtime unverändert)
   ▼
Staging  <DATA>/scan-staging/<inbox_id>/pending-review/   (Original sicher zwischengelagert)
   │  enqueue scan_ingest job (Move = atomare Claim-Operation)
   ▼
scan_ingest:  OCR (erzwungen) → Extraktion → Index (SqliteStore)
   │          + KI-Vorschlag Zielordner (NUR existierende Ordner der Ziel-Wurzel) + Tags
   │          + ai_decisions-Provenance, ACL = Admins + Zuständige (explizit, nicht public)
   ▼
scan_review (status=pending) ──► UI „Scan-Posteingang"
   │  Mensch bestätigt/ändert Zielordner & Tags
   ▼
„Apply & move file"-Logik (wiederverwendet): Datei → target_root/<Ordner>,
   Index-Pfad aktualisiert, Tags gesetzt, scan_review status=filed
```

## Konfiguration (`scan_inboxes`)

Neue Liste in `config.json`, **verwaltet über die Admin-UI** (Config → Scan-Eingänge):

```json
{
  "scan_inboxes": [
    {
      "id": "buchhaltung",
      "label": "Scan-Buchhaltung",
      "inbox_path": "/scans/buchhaltung",
      "target_root": "/documents/Buchhaltung",
      "reviewers": { "roles": ["accounting"], "users": ["m.muster"] },
      "stability_seconds": 300,
      "poll_interval_seconds": 60,
      "enabled": true
    }
  ]
}
```

| Feld | Bedeutung |
|---|---|
| `id` | stabiler Schlüssel; serverseitig aus `label` slugifiziert, eindeutig, danach unveränderlich |
| `label` | Anzeigename in der UI |
| `inbox_path` | überwachter Netzwerkordner (Eingang) |
| `target_root` | Ziel-Wurzel; Auswahl nur aus deren existierenden Unterordnern |
| `reviewers.roles` / `.users` | Zuständige zusätzlich zu Admins (steuert ACL + Queue-Sichtbarkeit) |
| `stability_seconds` | Ruhefenster bis „fertig geschrieben" (Default 300, Minimum 30) |
| `poll_interval_seconds` | Polling-Fallback-Takt (Events primär) |
| `enabled` | Watcher pro Eingang an/aus |

**Validierung beim Speichern** (fail-fast, klare Meldung): `id`/`label` nicht leer; `id`
eindeutig; `inbox_path` und `target_root` existieren und sind verschieden; `inbox_path` liegt
**nicht** innerhalb `target_root` (sonst würde abgelegtes Material erneut als Scan erkannt);
`stability_seconds ≥ 30`. Die Staging-Wurzel ist intern abgeleitet
(`<DATA>/scan-staging/<id>/`) und **nicht** frei konfigurierbar.

### Admin-UI „Scan-Eingänge" (Config)

- Neuer Bereich *Config → Scan-Eingänge* (Admin-only), analog zum *Paths*-Tab.
- CRUD pro Eingang: anlegen, bearbeiten, aktivieren/deaktivieren, löschen → schreibt
  `scan_inboxes` in `config.json`.
- Formularfelder: Label, Eingangsordner, Ziel-Wurzel, Zuständige (Rollen + Benutzer als
  Mehrfachauswahl aus vorhandenen Rollen/Usern), Stabilitäts-Fenster, Poll-Intervall, aktiv.
- **„Testen"-Button** pro Eingang (nutzt vorhandenes Path-Test-Muster): prüft Existenz,
  Les-/Schreibbarkeit und Validierungsregeln; Ergebnis als Success/Error-State in der UI.
- Änderungen wenden den Watcher **live** an (Start/Stop ohne Neustart), analog zum
  Reindex-Scheduler.

## Datenmodell: `scan_review`

| Spalte | Typ | Bedeutung |
|---|---|---|
| `id` | INTEGER PK | |
| `inbox_id` | TEXT | Zuordnung zum Eingang (aus Config) |
| `document_id` | INTEGER FK | das indexierte Dokument (OCR-Text durchsuchbar in Review) |
| `staging_path` | TEXT | aktueller Ablageort im Staging |
| `original_filename` | TEXT | ursprünglicher Scan-Name |
| `status` | TEXT | `pending` · `filed` · `rejected` · `error` |
| `suggested_folder` | TEXT | KI-Vorschlag (relativ zur `target_root`, nur existierender Ordner) |
| `suggested_tags` | TEXT (JSON) | KI-Tag-Vorschläge |
| `ai_reasoning` | TEXT | Begründung (Provenance) |
| `ai_decision_id` | INTEGER FK | Verweis auf `ai_decisions`-Zeile |
| `error_message` | TEXT | bei `status=error` |
| `created_at` / `updated_at` | TEXT (ISO) | |
| `reviewed_by` / `reviewed_at` | TEXT / TEXT | wer/wann bestätigt oder abgelehnt |

### Status-Lebenszyklus

```
pending ──confirm──► filed      (Datei verschoben, Index-Pfad aktualisiert, Tags gesetzt)
   │
   ├────reject──────► rejected   (Dokument aus Index entfernt; Original → scan-staging/rejected/,
   │                              nicht hart gelöscht)
   └──(ingest fail)─► error ──retry──► pending
```

### ACL (kritisch)

Der Scan erhält beim Ingest **explizite** ACL-Lesezeilen für Admins + die in der Config
hinterlegten Zuständigen. Er darf sich **nicht** auf „keine ACL-Zeile = privat" verlassen,
weil `_backfill_acl` bei jeder Store-Konstruktion public-read wieder herstellt
(siehe Memory `acl-backfill-republicizes`). Abgesichert durch einen Legacy-DB-Regressionstest.

### Migration

Additive `CREATE TABLE IF NOT EXISTS` + Index auf `(inbox_id, status)`. Die Index-Anlage
erfolgt **nach** etwaigen `ALTER`-Schritten (siehe Memory `schema-migration-ordering`) und wird
gegen die Wurzel-`document_index.db` getestet, nicht nur gegen `tmp_path`.

## Scan-Ingest-Job, OCR & eingeschränkter KI-Vorschlag

`scan_ingest`-Handler (registriert wie `index_paths`, eigener `SqliteStore` im Worker):

1. **Quelle**: eine stabile Datei im Staging.
2. **Extraktion mit erzwungener OCR**: Im Scan-Pfad ist OCR **immer** aktiv, unabhängig von der
   globalen Einstellung. Bild-PDFs via `pdf_extractor`+Tesseract, Bilddateien via neuem
   `image_extractor`. Fehlschlag → `status=error` mit Meldung (geloggt, kein stilles Schlucken).
3. **Index**: Dokument + Content-Blöcke wie beim normalen Indexlauf, damit der OCR-Text sofort
   durchsuchbar ist (auch in der Review-Vorschau).
4. **KI-Vorschlag (eingeschränkt & validiert)**: `ai_organizer.suggest` erhält die Liste der
   existierenden Unterordner der `target_root` als erlaubte Auswahl. Der Vorschlag wird via
   `ai_validation` gegen diese Liste geprüft; ein nicht existierender Ordner führt zum Fallback
   („kein Vorschlag / Wurzel"), nicht zur Übernahme eines erfundenen Pfads. Ergebnis +
   Begründung als `ai_decisions`-Provenance-Zeile, deren ID in `scan_review.ai_decision_id`
   referenziert wird. Einziger Test-Monkeypatch-Seam: `AiOrganizer._generate()`
   (siehe Memory `ai-validation-provenance`).
5. **ACL**: explizite Lesezeilen für Admins + Zuständige.
6. **Abschluss**: `scan_review`-Eintrag `status=pending`.

**Bestätigen (`confirm`)**: Wiederverwendung der vorhandenen „Apply & move file"-Bewegung —
Datei von Staging nach `target_root/<gewählter Ordner>`, Index-Pfad aktualisiert, gewählte Tags
gesetzt, `status=filed`. `ai_decisions` wird um die menschliche Entscheidung ergänzt
(angenommen/korrigiert) — wertvolles Feedback-Signal.

**`image_extractor`**: nimmt `.jpg/.png/.tiff`, liest/rendert das Bild, schickt es durch den
bestehenden `ocr_service`, liefert ein `ExtractionResult` mit einem `page`-Content-Block.
`.jpg/.png/.tiff` werden **nur für den Scan-Pfad** als unterstützt geführt; der normale Crawler
bleibt unverändert (kein globales Bild-Indexing — YAGNI).

## Review-UI & API

### UI (Ingest → Scan-Posteingang)

- Sichtbar nur für Admins + in `reviewers` hinterlegte Rollen/Benutzer.
- Eingang-Auswahl (Dropdown) + Statusfilter (`pending` / `error` / erledigt).
- Liste je Eintrag: Original-Dateiname, Eingangsdatum, KI-Vorschlag (Ordner + Tags +
  Begründung), Status.
- Detail/Vorschau: OCR-Textauszug (Snippet aus dem Index) + Aktionen:
  - **Zielordner**: Auswahl nur aus existierenden Unterordnern der `target_root`
    (KI-Vorschlag vorausgewählt) + bewusste Aktion „Neuen Ordner anlegen".
  - **Tags** editierbar (KI-Vorschlag vorbefüllt).
  - **Bestätigen** → verschieben + Index-Pfad aktualisieren → `filed`.
  - **Ablehnen** → `rejected`.
  - bei `error`: Fehlermeldung + **Erneut versuchen**.
- Konsistente Zustände (Loading / Empty / Error / Success) gemäß GUI-Leitlinien.

### API-Routen

Alle hinter Auth + Zuständigkeits-/Admin-Check; jede zustandsändernde Route ruft `_audit(...)`
am Erfolgspunkt (siehe Memory `audit-log-convention`).

| Methode & Pfad | Zweck |
|---|---|
| `GET /api/scan/inboxes` | konfigurierte Eingänge (für Auswahl), nach Zuständigkeit gefiltert |
| `GET /api/scan/review?inbox=&status=` | Queue-Einträge (ACL-gefiltert) |
| `GET /api/scan/review/{id}/folders` | existierende Unterordner der Ziel-Wurzel |
| `POST /api/scan/review/{id}/confirm` | `{folder, tags, new_folder?}` → verschieben & `filed` |
| `POST /api/scan/review/{id}/reject` | → `rejected` |
| `POST /api/scan/review/{id}/retry` | `error` → erneut einreihen |

**Sicherheit**: Zielordner serverseitig gegen `target_root` validieren (Pfad-Traversal-Schutz:
aufgelöster Zielpfad muss innerhalb `target_root` liegen). Die `/confirm`-Bewegung läuft über
eine ACL-prüfende Move-Methode, nie über direkten `conn.execute` (siehe Memory
`observability-and-shared-conn`).

## Watcher-Robustheit & Fehlerpfade

- **Events primär, Polling-Fallback** je Eingang: Liefert watchdog auf dem SMB-Share keine
  Events, holt der Poll (`poll_interval_seconds`) verpasste Dateien nach.
- **Stabilitätsprüfung**: Aufnahme erst, wenn Größe **und** mtime `stability_seconds`
  unverändert sind — schützt vor halb geschriebenen, mehrseitigen Scans.
- **Idempotenz / kein Doppel-Processing**: Vor dem Move per Content-Hash + Pfad prüfen; bereits
  aufgenommene Dateien überspringen. Der Move ins Staging ist die atomare Claim-Operation.
- **Recovery bei Neustart**: Interrupted-Running-Jobs werden neu markiert (bestehendes Muster);
  Dateien im Staging ohne `scan_review`-Eintrag werden beim Start erneut eingereiht.
- **Live-Reconfig**: Eingänge starten/stoppen Watcher-Threads ohne App-Neustart (analog Scheduler).

Fehlerpfade (explizit, geloggt):
- OCR/Extraktion fehlgeschlagen → `status=error`, Original bleibt im Staging, „Erneut versuchen".
- Ollama nicht erreichbar → Ingest läuft durch (Index + `pending`), nur **ohne** KI-Vorschlag;
  Timeouts/Fallback gemäß AGENTS.md.
- Ziel-Wurzel beim `confirm` verschwunden/nicht schreibbar → klarer Fehlerzustand, Eintrag
  bleibt `pending`.
- Nicht unterstütztes Format im Eingang → `status=error` mit Hinweis.

## Observability

Neue Metriken über die private `observability.REGISTRY` (nicht das globale Default-Registry,
siehe Memory `observability-and-shared-conn`): z. B. `scan_ingested_total{inbox,outcome}`,
`scan_review_pending`. Watcher-Heartbeat im Log.

## Tests (pytest)

- Stabilitätserkennung: „Datei wächst noch" vs. „stabil".
- `image_extractor` mit gemocktem `ocr_service`.
- KI-Vorschlag-Validierung: erfundener Ordner → Fallback (über `AiOrganizer._generate`-Seam).
- `confirm` verschiebt Datei + aktualisiert Index-Pfad + setzt `filed`; `reject` entfernt aus Index.
- **ACL-Regression**: Scan nur für Zuständige/Admins sichtbar — inkl. Legacy-DB-Lauf gegen die
  Wurzel-`document_index.db`, nicht nur `tmp_path`.
- Idempotenz: dieselbe Datei wird nicht zweimal aufgenommen.
- Pfad-Traversal beim `confirm` wird abgewiesen.
- Validierung der `scan_inboxes`-Config (eindeutige `id`, `inbox_path` nicht in `target_root`).

## Out of Scope

- Vollautomatische Ablage ohne Review (bewusst verworfen).
- Struktur-Analyse/-Verbesserung — bereits durch „AI: Folder Structure Suggestions" abgedeckt.
- Globales Bild-Indexing außerhalb des Scan-Pfads.
- Benachrichtigungen bei neuem Review-Eingang (mögliche spätere Erweiterung via `webhook_service`).
