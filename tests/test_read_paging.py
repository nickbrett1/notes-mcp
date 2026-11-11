"""notes_read paging: read long notes in chunks clients will not truncate."""

from __future__ import annotations

import pytest

from notes_mcp import server, vault
from notes_mcp.vault import VaultError


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("NOTES_VAULT", str(root))
    return root


def test_default_reads_the_whole_note(root):
    (root / "n.md").write_text("abcdef", encoding="utf-8")
    assert vault.read_note(vault.vault_root(), "n.md") == "abcdef"


def test_page_by_page_concatenates_to_the_whole_note(root):
    text = "".join(str(i % 10) for i in range(50_000))
    (root / "big.md").write_text(text, encoding="utf-8")

    chunks = []
    offset = 0
    while True:
        chunk = vault.read_note(vault.vault_root(), "big.md", offset=offset, limit=15_000)
        chunks.append(chunk)
        if len(chunk) < 15_000:
            break
        offset += 15_000

    assert "".join(chunks) == text


def test_offset_past_the_end_is_empty(root):
    (root / "n.md").write_text("abc", encoding="utf-8")
    assert vault.read_note(vault.vault_root(), "n.md", offset=99) == ""


def test_negative_offset_is_refused(root):
    (root / "n.md").write_text("abc", encoding="utf-8")
    with pytest.raises(VaultError):
        vault.read_note(vault.vault_root(), "n.md", offset=-1)


def test_zero_limit_is_refused(root):
    (root / "n.md").write_text("abc", encoding="utf-8")
    with pytest.raises(VaultError):
        vault.read_note(vault.vault_root(), "n.md", limit=0)


def test_tool_forwards_paging(root):
    (root / "n.md").write_text("abcdef", encoding="utf-8")
    assert server.notes_read("n.md", offset=2, limit=3) == "cde"
