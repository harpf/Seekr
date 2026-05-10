from pathlib import Path

from document_search.services.ai_organizer import AiOrganizer


def test_ai_organizer_placeholder():
    suggestion = AiOrganizer().suggest(file_path=Path('/tmp/a.pdf'), tags=['x'], metadata={'k':'v'})
    assert suggestion.reason
