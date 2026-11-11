"""Note size: unlimited by default, capped only when an operator opts in.

The old 51,200-character default made agents lose state on long notes, so the
default is now no limit. ``NOTES_MAX_CHARS`` still allows a deliberate ceiling.
"""

from __future__ import annotations

import pytest

from notes_mcp import vault
from notes_mcp.vault import NoteTooLargeError


def make_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def test_no_limit_by_default():
    assert vault.max_note_chars() is None


def test_large_write_succeeds_by_default(tmp_path):
    root = make_root(tmp_path)
    vault.write_note(root, "big.md", title="Big", body="x" * 200_000)
    assert (root / "big.md").exists()


def test_operator_can_still_impose_a_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "100")
    root = make_root(tmp_path)
    with pytest.raises(NoteTooLargeError):
        vault.write_note(root, "big.md", title="Big", body="x" * 500)
    vault.write_note(root, "small.md", title="S", body="hi")  # fits


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5", " "])
def test_invalid_or_non_positive_limit_means_unlimited(monkeypatch, bad):
    monkeypatch.setenv("NOTES_MAX_CHARS", bad)
    assert vault.max_note_chars() is None


def test_append_refused_when_it_would_exceed(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "200")
    root = make_root(tmp_path)
    vault.write_note(root, "n.md", title="N", body="a" * 50)
    with pytest.raises(NoteTooLargeError):
        vault.append_note(root, "n.md", "b" * 500)
