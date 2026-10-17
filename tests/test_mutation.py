"""Mutation ops: move, patch, archive, referrers, and cap handling."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from notes_mcp import frontmatter as fm
from notes_mcp import vault
from notes_mcp.vault import NoteTooLargeError, VaultPathError


def make_root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def write_raw(root, rel, meta=None, body="body\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if meta is None:
        path.write_text(body, encoding="utf-8")
    else:
        path.write_text(fm.emit(meta, body), encoding="utf-8")
    return path


# --- move -----------------------------------------------------------------


def test_move_preserves_bytes_and_created(tmp_path):
    root = make_root(tmp_path)
    write_raw(
        root,
        "foo.md",
        {"title": "Foo", "created": "2020-01-01T00:00:00Z", "updated": "2021-01-01T00:00:00Z"},
        "body\n",
    )
    before = (root / "foo.md").read_bytes()

    result = vault.move_note(root, "foo.md", "projects/reader/foo.md")

    assert result["from"] == "foo.md"
    assert result["to"] == "projects/reader/foo.md"
    assert not (root / "foo.md").exists()
    assert (root / "projects/reader/foo.md").read_bytes() == before


def test_move_refuses_to_clobber_without_overwrite(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "a.md", {"title": "A"})
    write_raw(root, "b.md", {"title": "B"})

    with pytest.raises(VaultPathError):
        vault.move_note(root, "a.md", "b.md")
    assert (root / "a.md").exists()

    vault.move_note(root, "a.md", "b.md", overwrite=True)
    assert not (root / "a.md").exists()
    meta, _ = fm.parse((root / "b.md").read_text(encoding="utf-8"))
    assert meta["title"] == "A"


def test_move_reports_inbound_references(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "reader-deploy.md", {"title": "Deploy"})
    write_raw(root, "one.md", {}, "see [[reader-deploy]] and [[reader-deploy|it]]\n")
    write_raw(root, "two.md", {}, "path `projects/reader/reader-deploy.md`\n")
    write_raw(root, "three.md", {}, "bare reader-deploy.md mention\n")

    result = vault.move_note(root, "reader-deploy.md", "reader-deploy-renamed.md")
    kinds = {(entry["kind"], entry["path"]) for entry in result["references"]}
    assert ("wikilink", "one.md") in kinds
    assert ("backticked_path", "two.md") in kinds
    assert ("mention", "three.md") in kinds


def test_move_refuses_legacy_tree_without_flag(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "Archived/old.md", {"title": "Old"})

    with pytest.raises(VaultPathError):
        vault.move_note(root, "Archived/old.md", "projects/reader/old.md")
    vault.move_note(
        root, "Archived/old.md", "projects/reader/old.md", allow_legacy=True
    )


# --- referrers ------------------------------------------------------------


def test_referrers_finds_the_two_backticked_paths(tmp_path):
    root = make_root(tmp_path)
    write_raw(
        root,
        "um790-litellm-cutover-handover.md",
        {"title": "Handover"},
    )
    write_raw(
        root,
        "um790-litellm-cutover-runbook.md",
        {},
        "See `um790-litellm-cutover-handover.md` before cutover.\n",
    )
    write_raw(
        root,
        "um790-phase-1-build-order.md",
        {},
        "Handover: `infra/um790/um790-litellm-cutover-handover.md`\n",
    )
    write_raw(root, "unrelated.md", {}, "nothing here\n")

    result = vault.referrers(root, "um790-litellm-cutover-handover.md")

    ticked = [r for r in result["referrers"] if r["kind"] == "backticked_path"]
    assert {r["path"] for r in ticked} == {
        "um790-litellm-cutover-runbook.md",
        "um790-phase-1-build-order.md",
    }
    assert all(isinstance(r["line"], int) and r["line"] >= 1 for r in ticked)


def test_referrers_classifies_embeds_and_mentions(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "diagram.md", {"title": "Diagram"})
    write_raw(root, "a.md", {}, "![[diagram]]\n")
    write_raw(root, "b.md", {}, "plain diagram.md in prose\n")

    result = vault.referrers(root, "diagram.md")
    kinds = {(r["path"], r["kind"]) for r in result["referrers"]}
    assert ("a.md", "embed") in kinds
    assert ("b.md", "mention") in kinds


# --- patch ----------------------------------------------------------------


def test_patch_frontmatter_works_on_an_over_cap_note(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "1000")
    root = make_root(tmp_path)
    big = write_raw(root, "big.md", None, "x" * 5000)

    result = vault.patch_note(root, "big.md", frontmatter={"title": "Big"})

    assert result["frontmatter_changed"] == ["title"]
    meta, body = fm.parse(big.read_text(encoding="utf-8"))
    assert meta["title"] == "Big"
    assert body.strip() == "x" * 5000


def test_patch_frontmatter_merges_and_never_invents_created(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "note.md", None, "body\n")

    vault.patch_note(
        root,
        "note.md",
        frontmatter={"title": "Note", "project": "reader", "type": "plan"},
    )

    meta, body = fm.parse((root / "note.md").read_text(encoding="utf-8"))
    assert meta["title"] == "Note"
    assert meta["project"] == "reader"
    assert meta["type"] == "plan"
    assert "created" not in meta
    assert body.strip() == "body"


def test_patch_none_deletes_a_frontmatter_key(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "note.md", {"title": "N", "status": "draft"}, "body\n")

    vault.patch_note(root, "note.md", frontmatter={"status": None})

    meta, _ = fm.parse((root / "note.md").read_text(encoding="utf-8"))
    assert "status" not in meta
    assert meta["title"] == "N"


def test_patch_replace_is_guarded_by_count(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "note.md", {"title": "N"}, "one two two\n")

    with pytest.raises(VaultPathError):
        vault.patch_note(root, "note.md", find="two", replacement="2")
    assert "two two" in (root / "note.md").read_text(encoding="utf-8")

    result = vault.patch_note(root, "note.md", find="two", replacement="2", count=0)
    assert result["replacements"] == 2
    assert "one 2 2" in (root / "note.md").read_text(encoding="utf-8")


def test_patch_replace_scoped_to_section(tmp_path):
    root = make_root(tmp_path)
    body = "# One\nkeep\n\n## Two\nchange me\n\n### Sub\nchange me\n\n## Three\nchange me\n"
    write_raw(root, "note.md", {"title": "N"}, body)

    result = vault.patch_note(
        root, "note.md", find="change me", replacement="changed", count=2, section="Two"
    )

    assert result["replacements"] == 2
    text = (root / "note.md").read_text(encoding="utf-8")
    assert text.count("changed") == 2
    assert text.count("change me") == 1  # the section after Two is untouched


def test_patch_replace_regex(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "note.md", {"title": "N"}, "date: 2026-10-17\n")

    vault.patch_note(
        root, "note.md", find=r"\d{4}-\d{2}-\d{2}", replacement="2026-10-18", regex=True
    )
    assert "2026-10-18" in (root / "note.md").read_text(encoding="utf-8")


def test_patch_that_grows_an_over_cap_note_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "1000")
    root = make_root(tmp_path)
    write_raw(root, "big.md", None, "x" * 5000)

    with pytest.raises(NoteTooLargeError):
        vault.patch_note(root, "big.md", find="x", replacement="x" * 6000, count=0)


# --- write frontmatter ----------------------------------------------------


def test_write_existing_without_created_uses_mtime_not_now(tmp_path):
    root = make_root(tmp_path)
    path = write_raw(root, "old.md", None, "old body\n")
    stamp = datetime(2026, 10, 12, 9, 0, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))

    vault.write_note(root, "old.md", title="Old", body="new body")

    meta, _ = fm.parse(path.read_text(encoding="utf-8"))
    assert meta["created"] == fm.iso_from_timestamp(stamp)


def test_write_existing_without_created_accepts_explicit_created(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "old.md", None, "old body\n")

    vault.write_note(
        root,
        "old.md",
        title="Old",
        body="new body",
        created="2026-10-12T09:00:00Z",
    )

    meta, _ = fm.parse((root / "old.md").read_text(encoding="utf-8"))
    assert meta["created"] == "2026-10-12T09:00:00Z"


def test_write_preserves_unknown_frontmatter_and_sets_new_fields(tmp_path):
    root = make_root(tmp_path)
    write_raw(
        root,
        "note.md",
        {"title": "N", "custom": "keep", "created": "2020-01-01T00:00:00Z"},
        "body\n",
    )

    vault.write_note(
        root,
        "note.md",
        title="New",
        body="body",
        tags=["t"],
        project="reader",
        note_type="plan",
        status="active",
    )

    meta, _ = fm.parse((root / "note.md").read_text(encoding="utf-8"))
    assert meta["custom"] == "keep"
    assert meta["project"] == "reader"
    assert meta["type"] == "plan"
    assert meta["status"] == "active"
    assert meta["created"] == "2020-01-01T00:00:00Z"


# --- archive --------------------------------------------------------------


def test_archive_mirrors_project_tree(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "projects/reader/reader-plan.md", {"title": "Plan"})

    result = vault.archive_note(root, "projects/reader/reader-plan.md")

    assert result["to"] == "archive/reader/reader-plan.md"
    assert (root / "archive/reader/reader-plan.md").exists()


def test_archive_refuses_notes_already_archived(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "archive/reader/reader-plan.md", {"title": "Plan"})

    with pytest.raises(VaultPathError):
        vault.archive_note(root, "archive/reader/reader-plan.md")


# --- list -----------------------------------------------------------------


def test_list_tree_excludes_archive_and_reports_lint(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "inbox/unfiled.md", None, "no frontmatter\n")
    write_raw(root, "projects/reader/reader-plan.md", {"title": "P", "project": "reader"})
    write_raw(root, "archive/reader/old.md", {"title": "Old"})
    write_raw(root, "Archived/legacy.md", {"title": "Legacy"})

    tree = vault.list_notes(root)

    assert tree["total"] == 2
    paths = {folder["path"] for folder in tree["folders"]}
    assert "archive/reader" not in paths
    assert "Archived" not in paths
    assert tree["inbox"]["depth"] == 1
    assert {"path": "inbox/unfiled.md", "missing": ["title", "type", "project"]} in tree[
        "missing_frontmatter"
    ]


def test_list_tree_reports_inbox_age(tmp_path):
    root = make_root(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat(timespec="seconds").replace("+00:00", "Z")
    write_raw(root, "inbox/old.md", {"title": "Old", "updated": old})

    tree = vault.list_notes(root)

    assert tree["inbox"]["oldest_path"] == "inbox/old.md"
    assert tree["inbox"]["oldest_age_days"] >= 20
    assert tree["inbox"]["over_14_days"] is True


def test_list_folder_returns_missing_fields(tmp_path):
    root = make_root(tmp_path)
    write_raw(root, "projects/reader/reader-plan.md", {"title": "P"})

    notes = vault.list_notes(root, folder="projects/reader")

    assert notes == [
        {
            "path": "projects/reader/reader-plan.md",
            "title": "P",
            "updated": notes[0]["updated"],
            "missing": ["type", "project"],
        }
    ]


def test_cap_error_names_the_largest_section(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_MAX_CHARS", "500")
    root = make_root(tmp_path)
    body = "# Small\nx\n\n# Big\n" + ("y" * 2000) + "\n"

    with pytest.raises(NoteTooLargeError) as caught:
        vault.write_note(root, "big.md", title="Big", body=body)

    message = str(caught.value)
    assert "largest sections" in message
    assert "'Big'" in message
    assert "notes_patch" in message
