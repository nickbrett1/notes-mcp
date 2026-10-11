"""Plain substring + tag search over the vault.

Deliberately not semantic (that is a later phase). This module does no
filesystem work of its own - it walks the vault through :mod:`notes_mcp.vault`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import vault

_SNIPPET_MAX = 200


def _snippet(note: vault.Note, needle: str) -> str:
    """The first line matching ``needle`` plus the next line, or a fallback."""
    lines = note.body.splitlines()
    index = next(
        (i for i, line in enumerate(lines) if needle and needle in line.lower()),
        None,
    )
    if index is None:
        first = next((line for line in lines if line.strip()), note.title)
        return first.strip()[:_SNIPPET_MAX]
    window = [line.strip() for line in lines[index : index + 2] if line.strip()]
    return " ".join(window)[:_SNIPPET_MAX]


def search_notes(
    root: Path,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Case-insensitive substring search over path/title/body, plus tags.

    A query matches a note when it is a substring of the note's vault-relative
    path, its title, or its body, or when it matches one of the note's tags
    (with or without a leading ``#``).
    """
    needle = query.strip().lower()
    tag_needle = needle.lstrip("#")

    results: list[dict[str, Any]] = []
    for note in vault.iter_notes(root):
        tags = [tag.lower() for tag in note.tags]
        matched = (
            needle in note.path.lower()
            or needle in note.title.lower()
            or (bool(needle) and needle in note.body.lower())
            or (bool(tag_needle) and any(tag_needle in tag for tag in tags))
        )
        if not matched:
            continue
        results.append(
            {
                "path": note.path,
                "title": note.title,
                "snippet": _snippet(note, needle),
                "updated": note.updated,
            }
        )
        if len(results) >= limit:
            break
    return results
