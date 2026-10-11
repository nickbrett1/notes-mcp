"""Slug rules, frontmatter round-trip, and overwrite preservation."""

from __future__ import annotations

import pytest

from notes_mcp import frontmatter as fm
from notes_mcp import vault


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Hello World", "hello-world"),
        ("  Spaced   Out  ", "spaced-out"),
        ("C++ & Python!", "c-python"),
        ("--dashes--", "dashes"),
        ("Already-Slugged", "already-slugged"),
        ("a" * 80, "a" * 60),
        ("!!!", ""),
    ],
)
def test_slug(title, expected):
    assert fm.slug(title) == expected


def test_slug_is_capped_after_collapsing():
    assert len(fm.slug("word " * 40)) <= 60


def test_parse_without_frontmatter_returns_body_as_is():
    meta, body = fm.parse("just a body\n")
    assert meta == {}
    assert body == "just a body\n"


def test_round_trip_parse_emit():
    meta = {
        "title": "Round Trip",
        "tags": ["a", "b"],
        "created": "2026-01-01T00:00:00Z",
        "updated": "2026-01-02T00:00:00Z",
        "aliases": ["memo-uid-1"],
    }
    body = "Body text.\n"
    parsed_meta, parsed_body = fm.parse(fm.emit(meta, body))
    assert parsed_meta == meta
    assert parsed_body == body


def test_write_creates_a_new_note(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    result = vault.write_note(root, "note.md", title="Note", body="hi")

    assert result == {"path": "note.md", "created_or_updated": "created"}
    meta, body = fm.parse((root / "note.md").read_text(encoding="utf-8"))
    assert meta["title"] == "Note"
    assert meta["created"] == meta["updated"]
    assert body.strip() == "hi"


def test_overwrite_preserves_created_and_aliases_and_bumps_updated(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    original = fm.emit(
        {
            "title": "Old",
            "tags": [],
            "created": "2020-01-01T00:00:00Z",
            "updated": "2020-01-01T00:00:00Z",
            "aliases": ["memo-uid-42"],
        },
        "old body\n",
    )
    (root / "note.md").write_text(original, encoding="utf-8")

    result = vault.write_note(
        root, "note.md", title="New", body="new body", tags=["x"]
    )
    assert result["created_or_updated"] == "updated"

    meta, body = fm.parse((root / "note.md").read_text(encoding="utf-8"))
    assert meta["created"] == "2020-01-01T00:00:00Z"
    assert meta["aliases"] == ["memo-uid-42"]
    assert meta["updated"] != "2020-01-01T00:00:00Z"
    assert meta["title"] == "New"
    assert meta["tags"] == ["x"]
    assert body.strip() == "new body"


def test_append_is_additive_and_bumps_updated(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    vault.write_note(root, "note.md", title="Note", body="first")

    result = vault.append_note(root, "note.md", "second")
    assert result["path"] == "note.md"
    assert result["updated"]

    _, body = fm.parse((root / "note.md").read_text(encoding="utf-8"))
    assert "first" in body
    assert "second" in body
    assert body.index("first") < body.index("second")
