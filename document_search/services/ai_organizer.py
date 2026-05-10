from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class OrganizationSuggestion:
    suggested_subpath: str | None = None
    suggested_tags: list[str] | None = None
    reason: str | None = None


class AiOrganizer:
    """Extension point for future Ollama/AI based document organization."""

    def suggest(self, *, file_path: Path, tags: list[str], metadata: dict[str, str]) -> OrganizationSuggestion:
        # Placeholder: currently returns no suggestion.
        return OrganizationSuggestion(reason="AI organizer not configured")
