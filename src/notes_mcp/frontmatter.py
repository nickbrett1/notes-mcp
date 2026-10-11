"""YAML frontmatter parse/emit, slugging, and UTC timestamps.

Pure string work - no filesystem access lives here (see ``vault``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import yaml

# Frontmatter is delimited by a leading ``---`` fence. After the closing fence
# any run of newlines is consumed so a blank line between the fence and the body
# does not become part of the body on round-trip.
_FENCE_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n)*(.*)\Z",
    re.DOTALL,
)

# Canonical key order on emit; unknown keys are appended in insertion order.
_ORDER = ("title", "tags", "created", "updated", "aliases")


class FrontmatterError(ValueError):
    """Raised when a frontmatter block looks like YAML but is not a mapping."""


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_timestamp(timestamp: float) -> str:
    """Format a POSIX timestamp (e.g. a file mtime) as UTC ISO-8601."""
    return (
        datetime.fromtimestamp(timestamp, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def slug(title: str) -> str:
    """Slugify a title: lowercase, non-alphanumerics to ``-``, collapsed and capped.

    The result is at most 60 characters and never starts or ends with ``-``. A
    title with no alphanumerics at all slugs to the empty string.
    """
    text = re.sub(r"[^a-z0-9]+", "-", title.lower())
    return text.strip("-")[:60].strip("-")


def parse(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(meta, body)``.

    A document without a leading frontmatter fence yields ``({}, text)`` - the
    body is returned unchanged, as the spec requires.
    """
    match = _FENCE_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        return {}, text
    return meta, match.group(2)


def emit(meta: dict[str, Any], body: str) -> str:
    """Render ``meta`` and ``body`` back into a frontmatter document."""
    ordered: dict[str, Any] = {}
    for key in _ORDER:
        if key in meta and meta[key] is not None:
            ordered[key] = meta[key]
    for key, value in meta.items():
        if key not in ordered and value is not None:
            ordered[key] = value
    dumped = yaml.safe_dump(
        ordered,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    head = f"---\n{dumped}\n---\n"
    body = body.rstrip("\n")
    return f"{head}\n{body}\n" if body else head
