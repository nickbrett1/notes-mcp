"""The note-size ceiling: refuse writes/appends that would bloat the context."""

from __future__ import annotations

import pytest

from notes_mcp import vault
from notes_mcp.vault import NoteTooLargeError


def make_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def test_write_refused_when_over_default_limit(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(NoteTooLargeError):
        vault.write_note(root, "big.md", title="Big", body="x" * 60_000)
    assert not (root / "big.md").exists()


def test_write_allowed_under_limit(tmp_path):
    root = make_root(tmp_path)
    result = vault.write_note(root, "ok.md", title="Ok", body="x" * 1_000)
    assert result["created_or_updated"] == "created"


def test_limit_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "100")
    root = make_root(tmp_path)
    with pytest.raises(NoteTooLargeError):
        vault.write_note(root, "big.md", title="Big", body="x" * 500)
    vault.write_note(root, "small.md", title="S", body="hi")  # fits


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5", " "])
def test_bad_limit_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("NOTES_MAX_CHARS", bad)
    assert vault.max_note_chars() == vault.DEFAULT_MAX_NOTE_CHARS


def test_append_refused_when_it_would_exceed(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "200")
    root = make_root(tmp_path)
    vault.write_note(root, "n.md", title="N", body="a" * 50)
    with pytest.raises(NoteTooLargeError):
        vault.append_note(root, "n.md", "b" * 500)
