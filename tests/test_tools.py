"""No-drift rule: each tool body must equal its underlying plain function.

Timestamps are frozen so any difference between a tool and the function it
delegates to is a real divergence, not clock skew.
"""

from __future__ import annotations

import asyncio

import pytest

from notes_mcp import frontmatter as fm
from notes_mcp import search, server, vault

FROZEN = "2026-10-11T00:00:00Z"


@pytest.fixture
def vault_dir(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("NOTES_VAULT", str(root))
    monkeypatch.setattr(fm, "now_iso", lambda: FROZEN)
    return root


def test_exactly_nine_tools_are_registered():
    tools = asyncio.run(server.mcp.list_tools())
    assert {t.name for t in tools} == {
        "notes_search",
        "notes_read",
        "notes_write",
        "notes_append",
        "notes_list",
        "notes_patch",
        "notes_move",
        "notes_archive",
        "notes_referrers",
    }


def test_no_delete_tool():
    tools = asyncio.run(server.mcp.list_tools())
    assert not any("delete" in t.name for t in tools)


def test_notes_search_matches_plain_function(vault_dir):
    (vault_dir / "a.md").write_text("hello\n", encoding="utf-8")
    assert server.notes_search("hello") == search.search_notes(
        vault.vault_root(), "hello"
    )


def test_notes_read_matches_plain_function(vault_dir):
    (vault_dir / "a.md").write_text("hello\n", encoding="utf-8")
    assert server.notes_read("a.md") == vault.read_note(vault.vault_root(), "a.md")


def test_notes_write_matches_plain_function(vault_dir):
    tool_result = server.notes_write(path="x.md", title="X", body="b")
    # Reset to the same "does not exist yet" state for the plain call, so both
    # observe a create rather than the tool's create followed by an overwrite.
    (vault_dir / "x.md").unlink()
    plain_result = vault.write_note(vault.vault_root(), "x.md", title="X", body="b")
    assert tool_result == plain_result == {
        "path": "x.md",
        "created_or_updated": "created",
    }


def test_notes_write_without_project_files_to_dated_inbox(vault_dir):
    result = server.notes_write(title="My Great Note", body="b")
    assert result["path"].startswith("inbox/")
    assert result["path"].endswith("-my-great-note.md")
    assert (vault_dir / result["path"]).exists()


def test_notes_write_routes_known_project(vault_dir):
    result = server.notes_write(
        title="Mutation Ops", project="notes-mcp", type="plan", body="b"
    )
    assert result["path"] == "projects/notes-mcp/mutation-ops.md"


def test_notes_write_routes_known_host_to_infra(vault_dir):
    result = server.notes_write(title="Cutover", project="um790", body="b")
    assert result["path"] == "infra/um790/cutover.md"


def test_notes_write_routes_decision_and_mailbox(vault_dir):
    decision = server.notes_write(title="Use Archive", type="decision", body="b")
    mailbox = server.notes_write(title="Ping", type="mailbox", body="b")
    assert decision["path"].startswith("decisions/")
    assert mailbox["path"] == "inbox/mailbox/ping.md"


def test_notes_append_matches_plain_function(vault_dir):
    server.notes_write(path="x.md", title="X", body="b")
    assert server.notes_append("x.md", "more") == vault.append_note(
        vault.vault_root(), "x.md", "more"
    )


def test_notes_list_matches_plain_function(vault_dir):
    (vault_dir / "a.md").write_text("hello\n", encoding="utf-8")
    assert server.notes_list() == vault.list_notes(vault.vault_root())
