# AGENTS.md

## Zweck und Produktvision
Dieses Repository entwickelt ein **Indexierungs- und Suchtool für Dokumente und Dateien**.
Ziel ist ein lokal betreibbares System, das Inhalte extrahiert, indexiert, semantisch auffindbar macht und mit Unterstützung lokaler LLMs sinnvoll strukturiert.

## Projektkontext
- Sprache: Python 3.11+
- Laufzeit: FastAPI + Jinja2 Templates + statische Assets
- Kernziel: zuverlässige Dokumentenaufnahme (Ingestion), Strukturierung, Suche und nachvollziehbare Ergebnisdarstellung
- Perspektive: lokale LLM-Integration für Klassifikation, Metadatenanreicherung und strukturierte Ablage

## Aktuelle Code-Struktur (Orientierung)
- `document_search/main.py`: Startpunkt der Anwendung
- `document_search/app.py`: Web- und API-Verhalten
- `document_search/crawler.py`: Aufnahme/Traversal von Datenquellen
- `document_search/extractors/`: Dateityp-spezifische Inhaltsextraktion (PDF, DOCX, PPTX, TXT, MD, Legacy Office)
- `document_search/index/`: Indexspeicher und Suchlogik
- `document_search/services/`: Dienste (z. B. OCR, Hashing, Dateizugriff, AI-Organisation)
- `document_search/web/templates/` + `document_search/web/static/`: GUI-Struktur (HTML/CSS/JS)
- `tests/`: Unit- und Integrationsnahe Tests

## Arbeitsstruktur für Agents
1. **Verstehen vor Ändern**
   - Betroffene Flows und Module identifizieren.
   - Bestehende Konventionen in angrenzenden Dateien übernehmen.
2. **Klein, sicher, nachvollziehbar liefern**
   - Kleine Diffs, klare Commit-Nachrichten, keine unnötigen Refactorings.
   - Keine Secrets, Tokens oder umgebungsabhängigen Pfade einführen.
3. **Produktionsreife priorisieren**
   - Fehlerpfade explizit behandeln, aussagekräftig loggen, Konfiguration sicher halten.
   - Typannotationen und verständliche Funktionsgrenzen verwenden.
4. **Validieren**
   - Bei Verhaltensänderungen Tests ergänzen/anpassen.
   - Relevante Checks ausführen und Ergebnisse transparent berichten.

## Architektur- und Qualitätsprinzipien
- **Ingestion-Pipeline klar trennen**: Crawling, Extraktion, Normalisierung, Indexierung, Suche.
- **LLM-Einsatz begrenzen und robust kapseln**:
  - Lokale Modelle nur über definierte Servicegrenzen ansprechen.
  - Timeouts, Retries und Fallbacks für nicht verfügbare Modelle vorsehen.
  - LLM-Ausgaben validieren (Schema/Format), bevor sie persistiert werden.
- **Nachvollziehbarkeit**: Herkunft von Metadaten und AI-Entscheidungen dokumentierbar halten.
- **Sicherheit**: Keine automatische Exfiltration; lokale Datenverarbeitung als Default.

## GUI/UX-Leitlinien (modern & sauber)
- UI-Komponenten in Templates klar strukturieren (Layout, Navigation, Inhalte trennen).
- Konsistente visuelle Hierarchie, klare Zustände (Loading, Empty, Error, Success).
- Suchergebnisse lesbar präsentieren: Snippets, Hervorhebungen, Quelle, Metadaten.
- Barrierearme und responsive Gestaltung bevorzugen.
- Frontend-Logik in `app.js` modular und wartbar halten; Styles in `styles.css` konsistent benennen.

## Python-Konventionen
- Python 3.11+, Type Hints für neue/angepasste Funktionen
- Klare Fehlerbehandlung, kein stilles Schlucken von Exceptions
- Logging statt `print` für operative Diagnosen
- Ruff-/PEP8-kompatibler Stil

## Tests und Checks
- Voller Lauf: `pytest -q`
- Gezielt: `pytest -q tests/test_search_service.py`
- Bei Importproblemen lokal mit gesetztem `PYTHONPATH` prüfen (z. B. `PYTHONPATH=. pytest -q`)

## Assistant-spezifische Dateien
- `AGENTS.md` (repo-weite Vorgaben)
- `CLAUDE.md` (Claude-spezifische Arbeitsweise)
- `.github/copilot-instructions.md` (GitHub Copilot-spezifische Hinweise)
