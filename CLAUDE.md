# CLAUDE.md

## Rolle im Projekt
Du unterstützt die Entwicklung eines lokal betriebenen Dokumenten-Indexierungs- und Suchsystems mit optionaler lokaler LLM-Unterstützung für Strukturierung und Metadatenanreicherung.

## Erwartete Arbeitsweise
- Lies vor Änderungen `AGENTS.md`, `README.md` und die direkt betroffenen Module.
- Arbeite entlang der bestehenden Architektur (Crawler → Extractors → Services → Index → Web).
- Schlage größere Refactorings nur bei klarem Mehrwert vor (Robustheit, Sicherheit, Wartbarkeit).
- Halte Diffs klein, testbar und leicht reviewbar.

## Qualitätscheck vor Abschluss
1. Verhalten verstanden und nur zielgerichtet geändert.
2. Fehlerpfade sind explizit behandelt und sinnvoll geloggt.
3. Keine Breaking Changes ohne klare Begründung/Migrationshinweis.
4. Bei Verhaltensänderung Tests ergänzt oder angepasst.
5. GUI-Änderungen folgen konsistenter, moderner Struktur (States, Lesbarkeit, Responsiveness).

## Bevorzugte Befehle
- Tests komplett: `pytest -q`
- Einzeltest: `pytest -q tests/test_search_service.py`
- Optional bei lokalen Importthemen: `PYTHONPATH=. pytest -q`
