"""Path-safety: the traversal table is the highest-value test in the suite."""

from __future__ import annotations

import os

import pytest

from notes_mcp.vault import VaultPathError, resolve_note


def make_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


@pytest.mark.parametrize(
    "rel",
    [
        "../outside.md",
        "sub/../../outside.md",
        "../../etc/passwd",
        "..%2foutside.md",
        "..%5coutside.md",
        "/etc/passwd",
        "/vault/../outside.md",
        "",
        "   ",
        ".obsidian/app.md",
        ".trash/old.md",
        ".livesync/state.md",
        "headless-vault-livesync-v2/data.md",
        "photos/@eaDir/img.md",
        "x@eaDir/y.md",
        "sub/.trash/note.md",
    ],
)
def test_resolve_note_rejects_unsafe_paths(tmp_path, rel):
    with pytest.raises(VaultPathError):
        resolve_note(make_root(tmp_path), rel)


def test_resolve_note_rejects_null_byte(tmp_path):
    with pytest.raises(VaultPathError):
        resolve_note(make_root(tmp_path), "note\x00.md")


def test_resolve_note_rejects_symlink_that_escapes(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, root / "link.md")

    with pytest.raises(VaultPathError):
        resolve_note(root, "link.md")


def test_resolve_note_accepts_a_plain_note(tmp_path):
    root = make_root(tmp_path)
    resolved = resolve_note(root, "sub/dir/note.md")
    assert resolved == (root / "sub" / "dir" / "note.md").resolve()


def test_resolve_note_accepts_internal_path_climb_that_stays_inside(tmp_path):
    root = make_root(tmp_path)
    resolved = resolve_note(root, "sub/../note.md")
    assert resolved == (root / "note.md").resolve()


def test_count_notes_counts_paths_without_loading_bodies(tmp_path, monkeypatch):
    """The health endpoint's count must not read/parse every note body."""
    from notes_mcp import vault

    root = make_root(tmp_path)
    for rel in ("a.md", "b.md", "sub/c.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("body", encoding="utf-8")
    internal = root / ".obsidian"
    internal.mkdir()
    (internal / "ignored.md").write_text("body", encoding="utf-8")

    def boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("count_notes must not read note bodies")

    monkeypatch.setattr(vault, "_load", boom)
    assert vault.count_notes(root) == 3
