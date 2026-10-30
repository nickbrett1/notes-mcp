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


# --- readable error surface ------------------------------------------------
#
# mcp's MCPServer treats a bare non-ToolError as a crash: the client receives
# only "Error executing tool <name>" and the real text goes to the server log.
# Every deliberate vault/frontmatter refusal must therefore arrive as a
# ToolError carrying the path, the exception class and the find snippet.

# The two revisions of `debt-collection-servicing-simulator-design` §0 that
# produced the bug report: the caller's stale heading, and the heading another
# writer had already put in the note. The patch failed because the find no
# longer existed - a legitimate refusal that reached the caller as 32 opaque
# characters.
STALE_HEADING = "## 0. Build-time prerequisite — Cloudflare access for agents"
CURRENT_HEADING = "## 0. Cloudflare access for agents — available, not a blocker"

NOTE_A = f"""---
title: Servicing simulator — design (hosted, Cloudflare Workers)
project: debt-collection
type: design
status: draft
tags:
- debt-collection
- simulator
created: '2026-10-29T01:10:02Z'
updated: '2026-10-29T22:56:55Z'
---

# Servicing simulator — design (hosted, Cloudflare Workers)

{CURRENT_HEADING}

**Status:** available. The Cloudflare MCP path is set up, so this is no longer a
build-time gate: the artifacts (Worker, D1, R2 bucket, secrets) can be created and
inspected by an agent rather than clicked into the dashboard.

## 1. What it is
"""


def _patch_error(arguments):
    """Call notes_patch through the MCP layer; return the raised ToolError."""
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as caught:
        asyncio.run(server.mcp.call_tool("notes_patch", arguments))
    return caught.value


def test_stale_find_reports_a_readable_error_not_a_masked_crash(vault_dir):
    path = "projects/debt-collection/debt-collection-servicing-simulator-design.md"
    (vault_dir / "projects/debt-collection").mkdir(parents=True)
    (vault_dir / path).write_text(NOTE_A, encoding="utf-8")

    error = _patch_error(
        {"path": path, "find": STALE_HEADING, "replacement": "## 0. Prerequisite"}
    )

    from mcp.server.mcpserver.exceptions import UnexpectedToolError

    # The framework's crash wrapper must NOT be what the caller sees.
    assert not isinstance(error, UnexpectedToolError)
    message = str(error)
    assert message != "Error executing tool notes_patch"
    assert path in message
    assert "VaultPathError" in message
    assert "found 0" in message
    assert "Build-time prerequisite" in message  # the find snippet


def test_identity_replace_for_an_absent_string_is_a_readable_refusal(vault_dir):
    # Attempt 5 in the report: find == replacement, but the word is not there.
    (vault_dir / "note.md").write_text(
        NOTE_A.replace(STALE_HEADING, CURRENT_HEADING), encoding="utf-8"
    )

    error = _patch_error(
        {"path": "note.md", "find": "prerequisite", "replacement": "prerequisite"}
    )

    assert "VaultPathError" in str(error)
    assert "'prerequisite'" in str(error)
    assert "note.md" in str(error)


def test_other_tools_surface_expected_errors_readably(vault_dir):
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    calls = [
        ("notes_read", {"path": "missing.md"}, "missing.md"),
        (
            "notes_patch",
            {"path": "missing.md", "find": "x", "replacement": "y"},
            "missing.md",
        ),
        ("notes_move", {"from_path": "../escape.md", "to_path": "x.md"}, "../escape.md"),
        ("notes_append", {"path": "missing.md", "text": "more"}, "missing.md"),
    ]
    for name, arguments, needle in calls:
        with pytest.raises(ToolError) as caught:
            asyncio.run(server.mcp.call_tool(name, arguments))
        assert not isinstance(caught.value, UnexpectedToolError)
        message = str(caught.value)
        assert message != f"Error executing tool {name}"
        assert needle in message


def test_frontmatter_only_patch_still_succeeds(vault_dir):
    (vault_dir / "note.md").write_text(NOTE_A, encoding="utf-8")
    result = server.notes_patch("note.md", frontmatter={"status": "draft"})
    assert result["replacements"] == 0
    assert result["frontmatter_changed"] == ["status"]
