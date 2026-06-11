"""Parsing + validation for the `scan_inboxes` config list. DB-free and pure so
it is fully unit-testable. Persistence is handled by the existing config save."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_MIN_STABILITY_SECONDS = 30


class ScanInboxConfigError(ValueError):
    """Raised when a scan inbox configuration is invalid."""


@dataclass(slots=True)
class ScanInbox:
    id: str
    label: str
    inbox_path: str
    target_root: str
    reviewers_groups: list[str] = field(default_factory=list)
    reviewers_users: list[str] = field(default_factory=list)
    stability_seconds: int = 300
    poll_interval_seconds: int = 60
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "inbox_path": self.inbox_path,
            "target_root": self.target_root,
            "reviewers": {"groups": self.reviewers_groups, "users": self.reviewers_users},
            "stability_seconds": self.stability_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "enabled": self.enabled,
        }


def slugify_id(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower())
    return s.strip("-")


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def parse_scan_inboxes(raw: object) -> list[ScanInbox]:
    """Parse the raw `scan_inboxes` config value into validated ScanInbox objects.

    Validates structural rules (id/label present, unique ids, sane numbers).
    Filesystem existence is checked separately by `validate_inbox_paths` (so unit
    tests and config load do not require the paths to exist)."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ScanInboxConfigError("scan_inboxes must be a list")

    inboxes: list[ScanInbox] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ScanInboxConfigError("each scan inbox must be an object")
        label = str(entry.get("label", "")).strip()
        if not label:
            raise ScanInboxConfigError("scan inbox label must not be empty")
        inbox_path = str(entry.get("inbox_path", "")).strip()
        target_root = str(entry.get("target_root", "")).strip()
        if not inbox_path or not target_root:
            raise ScanInboxConfigError(f"inbox '{label}' needs inbox_path and target_root")

        ib_id = str(entry.get("id", "")).strip() or slugify_id(label)
        if not ib_id:
            raise ScanInboxConfigError(f"inbox '{label}' produced an empty id")
        if ib_id in seen_ids:
            raise ScanInboxConfigError(f"duplicate scan inbox id: {ib_id}")
        seen_ids.add(ib_id)

        reviewers = entry.get("reviewers") or {}
        stability = int(entry.get("stability_seconds", 300))
        if stability < _MIN_STABILITY_SECONDS:
            raise ScanInboxConfigError(
                f"inbox '{ib_id}': stability_seconds must be >= {_MIN_STABILITY_SECONDS}"
            )
        poll = int(entry.get("poll_interval_seconds", 60))
        if poll < 5:
            raise ScanInboxConfigError(f"inbox '{ib_id}': poll_interval_seconds must be >= 5")

        inboxes.append(
            ScanInbox(
                id=ib_id,
                label=label,
                inbox_path=inbox_path,
                target_root=target_root,
                reviewers_groups=_as_str_list(reviewers.get("groups")),
                reviewers_users=_as_str_list(reviewers.get("users")),
                stability_seconds=stability,
                poll_interval_seconds=poll,
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return inboxes


def validate_inbox_paths(inbox: ScanInbox) -> None:
    """Filesystem-level validation used by the UI "Test" action and config save."""
    inbox_p = Path(inbox.inbox_path).resolve()
    target_p = Path(inbox.target_root).resolve()
    if not inbox_p.exists() or not inbox_p.is_dir():
        raise ScanInboxConfigError(f"inbox_path does not exist or is not a directory: {inbox.inbox_path}")
    if not target_p.exists() or not target_p.is_dir():
        raise ScanInboxConfigError(f"target_root does not exist or is not a directory: {inbox.target_root}")
    if inbox_p == target_p:
        raise ScanInboxConfigError("inbox_path and target_root must differ")
    if inbox_p.is_relative_to(target_p):
        raise ScanInboxConfigError("inbox_path must not be inside target_root (filed docs would be re-ingested)")
