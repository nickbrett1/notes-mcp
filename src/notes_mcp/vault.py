"""The only module that touches the notes vault filesystem.

Every path on the vault is built here, through :func:`resolve_note`, so the
whole security surface is one reviewable file. Nothing else imports ``pathlib``
to work with the vault.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import frontmatter as fm

# Directories that are infrastructure, never notes: LiveSync state, Obsidian
# config/trash. A note inside one is invisible to every tool.
INTERNAL_DIRS = {"headless-vault-livesync-v2", ".livesync", ".trash", ".obsidian"}

# Percent-encoded separators are rejected outright: a caller that double-decodes
# must not be able to smuggle a traversal past the resolve() check.
_ENCODED_SEPARATORS = ("%2f", "%5c")


class VaultError(Exception):
    """Base class for vault errors."""


class VaultPathError(VaultError, ValueError):
    """The requested path is not a safe, in-vault note path."""


class NoteNotFoundError(VaultError, FileNotFoundError):
    """The requested note does not exist in the vault."""


@dataclass
class Note:
    """A note read from disk, with its frontmatter already parsed."""

    path: str  # vault-relative, POSIX separators
    abs_path: Path
    title: str
    text: str  # the full document, frontmatter included
    meta: dict[str, Any]
    body: str
    tags: list[str] = field(default_factory=list)
    updated: str = ""


def vault_root() -> Path:
    """The vault root, from ``NOTES_VAULT`` (default ``/vault``)."""
    return Path(os.environ.get("NOTES_VAULT", "/vault"))


def _base(root: Path) -> Path:
    return root.resolve()


def resolve_note(root: Path, rel: str, *, write: bool = False) -> Path:
    """Resolve ``rel`` against ``root``, refusing anything outside the vault.

    Rejects: empty paths, null bytes, percent-encoded separators, absolute
    paths, anything that escapes ``root`` after ``..`` and symlink resolution,
    anything inside :data:`INTERNAL_DIRS`, and anything inside a Synology
    ``@eaDir``.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise VaultPathError("path must be a non-empty string")
    if "\x00" in rel:
        raise VaultPathError("path contains a null byte")
    lowered = rel.lower()
    if any(token in lowered for token in _ENCODED_SEPARATORS):
        raise VaultPathError("percent-encoded path separators are not allowed")
    if rel.startswith("/") or rel.startswith("\\"):
        raise VaultPathError("absolute paths are not allowed")

    base = _base(root)
    try:
        candidate = (base / rel).resolve()
    except (OSError, ValueError) as exc:
        raise VaultPathError(f"invalid path: {exc}") from exc

    try:
        parts = candidate.relative_to(base).parts
    except ValueError:
        raise VaultPathError("path escapes the vault") from None

    if any(part in INTERNAL_DIRS for part in parts):
        raise VaultPathError("path is inside an internal directory")
    if any(part.endswith("@eaDir") for part in parts):
        raise VaultPathError("synology @eaDir is not a note")
    return candidate


def _coerce_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [tag.strip() for tag in raw.split(",") if tag.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(tag) for tag in raw]
    return [str(raw)]


def _load(base: Path, path: Path) -> Note:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        meta, body = fm.parse(text)
    except fm.FrontmatterError:  # pragma: no cover - defensive
        meta, body = {}, text
    updated = meta.get("updated")
    if not isinstance(updated, str) or not updated:
        updated = fm.iso_from_timestamp(path.stat().st_mtime)
    return Note(
        path=path.relative_to(base).as_posix(),
        abs_path=path,
        title=str(meta.get("title") or path.stem),
        text=text,
        meta=meta,
        body=body,
        tags=_coerce_tags(meta.get("tags")),
        updated=updated,
    )


def _iter_note_paths(base: Path, start: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(start):
        # Prune internal and Synology metadata directories in place so os.walk
        # never descends into them.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in INTERNAL_DIRS and not name.endswith("@eaDir")
        )
        for name in sorted(filenames):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            # A symlinked file must not point out of the vault.
            try:
                path.resolve().relative_to(base)
            except (OSError, ValueError):
                continue
            yield path


def iter_notes(root: Path, folder: str | None = None) -> Iterator[Note]:
    """Yield every note under ``root`` (or ``folder``), skipping internal dirs."""
    base = _base(root)
    start = base
    if folder:
        start = resolve_note(base, folder)
    if not start.exists():
        return
    if start.is_file():
        start = start.parent
    if not start.is_dir():  # pragma: no cover - guarded above
        return
    for path in _iter_note_paths(base, start):
        yield _load(base, path)


def count_notes(root: Path) -> int:
    """Number of addressable notes in the vault (health endpoint)."""
    return sum(1 for _ in iter_notes(root))


def read_note(root: Path, rel: str) -> str:
    """Return a note's full text (frontmatter + body), by vault-relative path."""
    path = resolve_note(root, rel)
    if path.is_dir():
        raise VaultPathError("path is a directory, not a note")
    if not path.exists():
        raise NoteNotFoundError(f"note not found: {rel}")
    return path.read_text(encoding="utf-8")


def write_note(
    root: Path,
    rel: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Create or overwrite a note. Preserves ``created`` and ``aliases``.

    Returns ``{"path": <relative path>, "created_or_updated": "created"|"updated"}``.
    """
    path = resolve_note(root, rel, write=True)
    existed = path.exists()
    if existed and path.is_dir():
        raise VaultPathError("target is a directory, not a note")

    existing: dict[str, Any] = {}
    if existed:
        try:
            existing, _ = fm.parse(path.read_text(encoding="utf-8", errors="replace"))
        except fm.FrontmatterError:  # pragma: no cover - defensive
            existing = {}

    meta: dict[str, Any] = {
        "title": title,
        "tags": list(tags or []),
        "created": existing.get("created") or fm.now_iso(),
        "updated": fm.now_iso(),
    }
    if existing.get("aliases"):
        meta["aliases"] = existing["aliases"]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fm.emit(meta, body), encoding="utf-8")
    return {
        "path": path.relative_to(_base(root)).as_posix(),
        "created_or_updated": "updated" if existed else "created",
    }


def append_note(root: Path, rel: str, text: str) -> dict[str, Any]:
    """Append ``text`` to an existing note as a new paragraph; bump ``updated``."""
    path = resolve_note(root, rel, write=True)
    if path.is_dir():
        raise VaultPathError("target is a directory, not a note")
    if not path.exists():
        raise NoteNotFoundError(f"note not found: {rel}")

    meta, body = fm.parse(path.read_text(encoding="utf-8", errors="replace"))
    meta = dict(meta)
    meta.setdefault("title", path.stem)
    updated = fm.now_iso()
    meta["updated"] = updated

    body = body.rstrip("\n")
    addition = text.rstrip("\n")
    new_body = f"{body}\n\n{addition}" if body else addition
    path.write_text(fm.emit(meta, new_body), encoding="utf-8")
    return {
        "path": path.relative_to(_base(root)).as_posix(),
        "updated": updated,
    }


def list_notes(
    root: Path,
    folder: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Browse notes as ``{path, title, updated}`` dicts, newest-walk order."""
    notes: list[dict[str, Any]] = []
    for note in iter_notes(root, folder):
        notes.append(
            {"path": note.path, "title": note.title, "updated": note.updated}
        )
        if len(notes) >= limit:
            break
    return notes
