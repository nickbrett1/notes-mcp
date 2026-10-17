"""The only module that touches the notes vault filesystem.

Every path on the vault is built here, through :func:`resolve_note`, so the
whole security surface is one reviewable file. Nothing else imports ``pathlib``
to work with the vault.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import frontmatter as fm

# Directories that are infrastructure, never notes: LiveSync state, Obsidian
# config/trash. A note inside one is invisible to every tool.
INTERNAL_DIRS = {"headless-vault-livesync-v2", ".livesync", ".trash", ".obsidian"}

# Archive trees are omitted from the default listing/tree. They are still
# readable and searchable; pass ``folder`` explicitly to browse one.
ARCHIVE_DIRS = {"archive", "Archived"}
LEGACY_ARCHIVE = ("Archived", "archive", "_legacy-memos-export")

# Hosts that live under ``infra/<host>/`` rather than ``projects/<host>/``
# (convention §10). Kept here so path derivation is self-contained.
INFRA_HOSTS = {
    "um790",
    "mac-studio",
    "nas",
    "litellm",
    "mcphub",
    "doppler",
    "buildkite",
    "tailscale",
    "pihole",
    "netboot",
}

# Percent-encoded separators are rejected outright: a caller that double-decodes
# must not be able to smuggle a traversal past the resolve() check.
_ENCODED_SEPARATORS = ("%2f", "%5c")

# A note's full text (frontmatter + body) is capped so an agent cannot write a
# single note far larger than it needs to, which bloats the context of every
# later reader. Mirrors the 51,200-char ceiling of the memos system this
# replaces. Override with NOTES_MAX_CHARS.
DEFAULT_MAX_NOTE_CHARS = 51_200


class VaultError(Exception):
    """Base class for vault errors."""


class VaultPathError(VaultError, ValueError):
    """The requested path is not a safe, in-vault note path."""


class NoteNotFoundError(VaultError, FileNotFoundError):
    """The requested note does not exist in the vault."""


class NoteTooLargeError(VaultError, ValueError):
    """A write/append would exceed the configured note-size ceiling."""


def max_note_chars() -> int:
    """Character ceiling for a note's full text, from ``NOTES_MAX_CHARS``.

    Falls back to :data:`DEFAULT_MAX_NOTE_CHARS` when unset or not a positive
    integer, so a bad value can never disable the guard entirely.
    """
    raw = os.environ.get("NOTES_MAX_CHARS")
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_NOTE_CHARS
        if value > 0:
            return value
    return DEFAULT_MAX_NOTE_CHARS


def _top_sections(text: str, limit: int = 3) -> list[tuple[str, int]]:
    """The largest markdown sections in ``text``, by character count.

    Used only for the cap diagnostic: it turns "too big" into "this section is
    the problem", which is what an agent needs to split sensibly.
    """
    try:
        _, body = fm.parse(text)
    except fm.FrontmatterError:  # pragma: no cover - defensive
        body = text
    lines = body.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    sections: list[tuple[str, int]] = []
    for pos, (start, _level, name) in enumerate(headings):
        end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
        sections.append((name, sum(len(line) for line in lines[start:end])))
    sections.sort(key=lambda item: item[1], reverse=True)
    return sections[:limit]


def _enforce_size(
    text: str,
    previous_text: str | None = None,
    *,
    note_path: str | None = None,
) -> None:
    """Refuse text over the cap, with a diagnostic that helps split it.

    ``previous_text`` is the note's pre-edit text. A mutation that does not
    grow an already-over-cap note is allowed: that is what makes a
    frontmatter-only ``notes_patch`` work on a note that can no longer be
    rewritten whole (work order §4.2/§4.3).
    """
    limit = max_note_chars()
    if len(text) <= limit:
        return
    if previous_text is not None and len(text) <= len(previous_text):
        return

    where = f" ({note_path})" if note_path else ""
    parts = [
        f"note{where} would be {len(text)} characters, over the {limit}-character limit"
    ]
    if previous_text is not None:
        parts.append(f"the current note is {len(previous_text)} characters")
    sections = _top_sections(text)
    if sections:
        parts.append(
            "largest sections: "
            + ", ".join(f"{name!r} ({size} chars)" for name, size in sections)
        )
    parts.append(
        "use notes_patch for a frontmatter-only edit, or split per "
        "_meta/convention.md §8.3 into <slug>-part-2.md with type: record "
        "and part_of: <original path>"
    )
    raise NoteTooLargeError("; ".join(parts))


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
    """Number of addressable notes in the vault (health endpoint).

    Counts note *paths* only: it never reads or parses note bodies, so a probe
    that runs every 30s (the container healthcheck, Homepage's site monitor)
    does not re-read the whole vault off slow storage on every call. The set of
    paths enumerated here is exactly what :func:`iter_notes` would yield.
    """
    base = _base(root)
    if not base.is_dir():
        return 0
    return sum(1 for _ in _iter_note_paths(base, base))


def read_note(root: Path, rel: str) -> str:
    """Return a note's full text (frontmatter + body), by vault-relative path."""
    path = resolve_note(root, rel)
    if path.is_dir():
        raise VaultPathError("path is a directory, not a note")
    if not path.exists():
        raise NoteNotFoundError(f"note not found: {rel}")
    return path.read_text(encoding="utf-8")


def derive_path(
    title: str,
    project: str | None = None,
    note_type: str | None = None,
    *,
    today: str | None = None,
) -> str:
    """Derive a convention-compliant vault path from ``project`` + ``type``.

    Tier 2 of `_meta/convention.md` §9: never vault root. Known ``type`` values
    route first (mailbox, decision, dated record/findings); a known project
    goes to ``projects/<p>/`` and a known host to ``infra/<host>/``. Anything
    without a project lands in ``inbox/YYYY-MM-DD-<slug>.md``.
    """
    slug = fm.slug(title) or "untitled"
    stamp = today or datetime.now(UTC).date().isoformat()

    if note_type == "mailbox":
        return f"inbox/mailbox/{slug}.md"
    if note_type == "decision":
        return f"decisions/{stamp}-{slug}.md"
    if note_type in {"record", "findings"} and not re.search(r"\d{4}-\d{2}-\d{2}$", slug):
        slug = f"{slug}-{stamp}"
    if project:
        folder = "infra" if project in INFRA_HOSTS else "projects"
        return f"{folder}/{project}/{slug}.md"
    return f"inbox/{stamp}-{slug}.md"


def write_note(
    root: Path,
    rel: str,
    title: str = "",
    body: str = "",
    tags: list[str] | None = None,
    *,
    project: str | None = None,
    note_type: str | None = None,
    status: str | None = None,
    created: str | None = None,
) -> dict[str, Any]:
    """Create or overwrite a note. Preserves existing frontmatter and ``created``.

    New notes get ``created = now``. An *existing* note with no ``created``
    never gets the current time invented for it: an explicit ``created`` wins,
    otherwise the file mtime is used, otherwise the field is omitted (work
    order §3). ``aliases`` and unknown keys survive an overwrite.
    """
    path = resolve_note(root, rel, write=True)
    existed = path.exists()
    if existed and path.is_dir():
        raise VaultPathError("target is a directory, not a note")

    previous_text: str | None = None
    existing: dict[str, Any] = {}
    if existed:
        previous_text = path.read_text(encoding="utf-8", errors="replace")
        try:
            existing, _ = fm.parse(previous_text)
        except fm.FrontmatterError:  # pragma: no cover - defensive
            existing = {}

    meta: dict[str, Any] = dict(existing)
    if title:
        meta["title"] = title
    if tags is not None:
        meta["tags"] = list(tags)
    for key, value in (("project", project), ("type", note_type), ("status", status)):
        if value is not None:
            if value == "":
                meta.pop(key, None)
            else:
                meta[key] = value

    if not meta.get("created"):
        if created:
            meta["created"] = created
        elif existed:
            meta["created"] = fm.iso_from_timestamp(path.stat().st_mtime)
        else:
            meta["created"] = fm.now_iso()
    # Never overwrite an existing ``created``; an explicit value only fills the
    # gap handled above.

    meta["updated"] = fm.now_iso()
    rendered = fm.emit(meta, body)
    _enforce_size(rendered, previous_text, note_path=rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
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

    previous_text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = fm.parse(previous_text)
    meta = dict(meta)
    updated = fm.now_iso()
    meta["updated"] = updated

    body = body.rstrip("\n")
    addition = text.rstrip("\n")
    new_body = f"{body}\n\n{addition}" if body else addition
    rendered = fm.emit(meta, new_body)
    _enforce_size(rendered, previous_text, note_path=rel)
    path.write_text(rendered, encoding="utf-8")
    return {
        "path": path.relative_to(_base(root)).as_posix(),
        "updated": updated,
    }


def _section_bounds(body: str, section: str) -> tuple[str, str, str]:
    """Split ``body`` into (before, section, after) around a heading match.

    ``section`` may be the full heading text (``7.2 Triage``), a prefix
    (``7.2``), or ``## 7.2 Triage``. The section ends at the next heading of
    the same or higher level.
    """
    wanted = section.strip().lstrip("#").strip()
    if not wanted:
        raise VaultPathError("section must be a heading name")
    lines = body.splitlines(keepends=True)
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            continue
        heading = match.group(2).strip()
        if heading == wanted or heading.startswith(wanted):
            start = index
            level = len(match.group(1))
            break
    if start is None:
        raise VaultPathError(f"section not found: {section}")

    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "".join(lines[:start]), "".join(lines[start:end]), "".join(lines[end:])


def _apply_replace(
    segment: str,
    find: str,
    replacement: str,
    *,
    count: int = 1,
    regex: bool = False,
) -> tuple[str, int]:
    """Replace ``find`` in ``segment``, guarded by the expected match count.

    ``count`` is the *expected* number of matches (default 1); ``0`` means
    "replace all, but require at least one". A mismatch changes nothing.
    """
    if not find:
        raise VaultPathError("find must be a non-empty string")
    if regex:
        try:
            found = len(re.findall(find, segment, flags=re.MULTILINE))
        except re.error as exc:
            raise VaultPathError(f"invalid regular expression: {exc}") from exc
    else:
        found = segment.count(find)

    if count == 0:
        if found == 0:
            raise VaultPathError(f"no match for {find!r}")
    elif found != count:
        raise VaultPathError(
            f"expected {count} match(es) for {find!r}, found {found}; nothing changed"
        )

    if regex:
        return re.sub(find, replacement, segment, count=count, flags=re.MULTILINE), found
    return segment.replace(find, replacement, -1 if count == 0 else count), found


def patch_note(
    root: Path,
    rel: str,
    *,
    frontmatter: dict[str, Any] | None = None,
    find: str | None = None,
    replacement: str = "",
    count: int = 1,
    section: str | None = None,
    regex: bool = False,
) -> dict[str, Any]:
    """Targeted note mutation: merge frontmatter and/or replace a substring.

    A ``frontmatter`` merge never touches the body and never invents
    ``created``; ``None`` deletes a key. ``find``/``replacement`` edit the body
    (optionally scoped to a heading) with an expected-match guard. The note's
    ``updated`` field is bumped.

    A patch is exempt from the size cap when it does not grow the note: this is
    what lets an over-cap note receive a frontmatter-only fix.
    """
    path = resolve_note(root, rel, write=True)
    if path.is_dir():
        raise VaultPathError("target is a directory, not a note")
    if not path.exists():
        raise NoteNotFoundError(f"note not found: {rel}")
    if frontmatter is None and find is None:
        raise VaultPathError("patch needs frontmatter and/or find")

    previous_text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = fm.parse(previous_text)
    meta = dict(meta)
    changed_frontmatter: list[str] = []

    if frontmatter is not None:
        if not isinstance(frontmatter, dict):
            raise VaultPathError("frontmatter must be a mapping")
        for key, value in frontmatter.items():
            if value is None:
                if key in meta:
                    del meta[key]
            else:
                meta[key] = value
            changed_frontmatter.append(str(key))

    replacements = 0
    body_changed = find is not None
    if find is not None:
        if section is not None:
            before, scoped, after = _section_bounds(body, section)
            scoped, replacements = _apply_replace(
                scoped, find, replacement, count=count, regex=regex
            )
            body = before + scoped + after
        else:
            body, replacements = _apply_replace(
                body, find, replacement, count=count, regex=regex
            )

    if meta:
        meta["updated"] = fm.now_iso()
        rendered = fm.emit(meta, body)
    else:
        # No frontmatter existed and none was requested: keep the body's raw
        # shape rather than introducing a frontmatter block just to bump a
        # timestamp that was never there.
        rendered = body

    if body_changed:
        # The cap exists to stop body bloat. A frontmatter-only patch is tiny
        # and is the documented escape hatch for an over-cap note, so it is
        # never refused; a body edit must not grow the note past the cap.
        _enforce_size(rendered, previous_text, note_path=rel)
    path.write_text(rendered, encoding="utf-8")
    return {
        "path": path.relative_to(_base(root)).as_posix(),
        "frontmatter_changed": changed_frontmatter,
        "replacements": replacements,
    }


def _require_legacy_allowed(rel_from: str, rel_to: str, allow_legacy: bool) -> None:
    for rel in (rel_from, rel_to):
        parts = PurePosixPath(rel).parts
        if allow_legacy:
            return
        if parts and parts[0] == "Archived":
            raise VaultPathError(
                f"refusing to mutate the quarantined legacy tree: {rel}; "
                "pass allow_legacy=true if this is intentional"
            )
        if len(parts) >= 2 and parts[0] == "archive" and parts[1] == "_legacy-memos-export":
            raise VaultPathError(
                f"refusing to mutate the quarantined legacy tree: {rel}; "
                "pass allow_legacy=true if this is intentional"
            )


def referrers(root: Path, rel: str) -> dict[str, Any]:
    """Find notes that reference ``rel``, with line numbers and confidence.

    Kinds: ``wikilink`` (``[[basename]]`` incl. alias/heading), ``embed``
    (``![[basename]]``), ``backticked_path`` (``<basename>.md`` or
    ``<sub/path>.md``), and ``mention`` (a bare ``<basename>.md`` in prose, the
    lowest-confidence kind and never safe to auto-rewrite).
    """
    base = _base(root)
    source = resolve_note(root, rel)
    wanted = source.relative_to(base).as_posix()
    basename = PurePosixPath(wanted).stem

    link_re = re.compile(
        rf"(!?)\[\[\s*{re.escape(basename)}(?:\.md)?(?=[#|\]])[^\]]*\]\]"
    )
    tick_re = re.compile(r"`([^`\n]+)`")
    mention_re = re.compile(rf"(?<![\w/-]){re.escape(basename)}\.md\b")

    entries: list[dict[str, Any]] = []
    kinds: dict[str, int] = {"wikilink": 0, "embed": 0, "backticked_path": 0, "mention": 0}

    for path in _iter_note_paths(base, base):
        note_rel = path.relative_to(base).as_posix()
        if note_rel == wanted:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover - race with a deleted file
            continue
        for number, line in enumerate(lines, start=1):
            found: set[str] = set()
            for match in link_re.finditer(line):
                kind = "embed" if match.group(1) else "wikilink"
                found.add(kind)
            for match in tick_re.finditer(line):
                content = match.group(1).strip()
                if (
                    content == wanted
                    or content == f"{basename}.md"
                    or content.endswith(f"/{basename}.md")
                ):
                    found.add("backticked_path")
            if not found and mention_re.search(line):
                found.add("mention")
            for kind in ("wikilink", "embed", "backticked_path", "mention"):
                if kind in found:
                    kinds[kind] += 1
                    entries.append(
                        {
                            "path": note_rel,
                            "line": number,
                            "kind": kind,
                            "text": line.strip(),
                        }
                    )

    return {
        "path": wanted,
        "basename": basename,
        "count": len(entries),
        "by_kind": kinds,
        "referrers": entries,
    }


def move_note(
    root: Path,
    from_rel: str,
    to_rel: str,
    *,
    overwrite: bool = False,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Move/rename a note within the vault without touching its bytes.

    Frontmatter (including ``created``) travels untouched. The result includes
    every inbound reference found before the move, so the caller can decide
    what will dangle: a folder move is safe for ``[[wikilinks]]`` but breaks
    backticked paths; a rename breaks both.
    """
    _require_legacy_allowed(from_rel, to_rel, allow_legacy)
    source = resolve_note(root, from_rel, write=True)
    target = resolve_note(root, to_rel, write=True)
    if source.is_dir():
        raise VaultPathError("source is a directory, not a note")
    if not source.exists():
        raise NoteNotFoundError(f"note not found: {from_rel}")
    if source == target:
        raise VaultPathError("source and target are the same path")
    if target.exists() and not overwrite:
        raise VaultPathError(f"target already exists: {to_rel}; pass overwrite=true")
    if target.is_dir():
        raise VaultPathError("target is a directory, not a note")

    references = referrers(root, from_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        os.replace(source, target)
    else:
        source.rename(target)
    top = _base(root)
    return {
        "from": source.relative_to(top).as_posix(),
        "to": target.relative_to(top).as_posix(),
        "references": references["referrers"],
    }


def _archive_target(rel: str) -> str:
    """``archive/<project|host>/<basename>``, mirroring the convention tree."""
    parts = PurePosixPath(rel).parts
    basename = parts[-1]
    if parts[0] in {"projects", "infra"} and len(parts) >= 3:
        return f"archive/{parts[1]}/{basename}"
    if parts[0] in {"inbox", "decisions"} and len(parts) >= 2:
        return f"archive/{parts[0]}/{basename}"
    return f"archive/{basename}"


def archive_note(
    root: Path,
    rel: str,
    *,
    overwrite: bool = False,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    """Move a note into ``archive/`` (archive-over-delete, convention §9.4).

    The legacy ``Archived/`` (capital A) tree is never an archive target: it is
    the quarantined Memos generation and stays where it is.
    """
    parts = PurePosixPath(rel).parts
    if parts and parts[0] in ARCHIVE_DIRS:
        raise VaultPathError(f"note is already archived: {rel}")
    target = _archive_target(rel)
    return move_note(
        root, rel, target, overwrite=overwrite, allow_legacy=allow_legacy
    )


def _missing_frontmatter(meta: dict[str, Any]) -> list[str]:
    return [key for key in ("title", "type", "project") if not meta.get(key)]


def _age_days(stamp: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max((datetime.now(UTC) - parsed).days, 0)


def list_notes(
    root: Path,
    folder: str | None = None,
    limit: int = 50,
) -> Any:
    """Browse notes, or return the folder tree when no ``folder`` is given.

    With ``folder``: a list of ``{path, title, updated, missing}`` where
    ``missing`` names any of ``title`` / ``type`` / ``project`` not set — the
    cheap frontmatter lint.

    Without ``folder``: ``{total, folders, inbox, missing_frontmatter}``. The
    folder tree carries per-folder note counts and missing-frontmatter counts;
    ``inbox`` carries depth and oldest-note age (convention §7.3). ``archive/``
    and the legacy ``Archived/`` are omitted unless browsed explicitly.
    """
    if folder:
        notes: list[dict[str, Any]] = []
        for note in iter_notes(root, folder):
            notes.append(
                {
                    "path": note.path,
                    "title": note.title,
                    "updated": note.updated,
                    "missing": _missing_frontmatter(note.meta),
                }
            )
            if len(notes) >= limit:
                break
        return notes

    base = _base(root)
    folders: dict[str, dict[str, Any]] = {}
    missing_frontmatter: list[dict[str, Any]] = []
    inbox: list[Note] = []
    mailbox_depth = 0
    total = 0

    for path in _iter_note_paths(base, base):
        rel = path.relative_to(base).as_posix()
        parts = PurePosixPath(rel).parts
        if parts and parts[0] in ARCHIVE_DIRS:
            continue
        note = _load(base, path)
        total += 1
        parent = posixpath.dirname(rel) or "."
        stats = folders.setdefault(
            parent,
            {
                "path": parent,
                "notes": 0,
                "missing_title": 0,
                "missing_type": 0,
                "missing_project": 0,
            },
        )
        stats["notes"] += 1
        missing = _missing_frontmatter(note.meta)
        for key in missing:
            stats[f"missing_{key}"] += 1
        if missing:
            missing_frontmatter.append({"path": rel, "missing": missing})
        if rel.startswith("inbox/"):
            if rel.startswith("inbox/mailbox/"):
                mailbox_depth += 1
            else:
                inbox.append(note)

    oldest = max(inbox, key=lambda note: note.updated, default=None)
    oldest_age = _age_days(oldest.updated) if oldest else None
    return {
        "total": total,
        "folders": sorted(folders.values(), key=lambda item: item["path"]),
        "inbox": {
            "depth": len(inbox),
            "mailbox_depth": mailbox_depth,
            "oldest_path": oldest.path if oldest else None,
            "oldest_age_days": oldest_age,
            "over_14_days": bool(oldest_age is not None and oldest_age > 14),
        },
        "missing_frontmatter": missing_frontmatter,
    }
