"""Plain substring + tag search, limiting, snippet shape, internal-dir exclusion."""

from __future__ import annotations

from notes_mcp import frontmatter as fm
from notes_mcp import search


def build_vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Archived").mkdir(parents=True)
    (root / ".obsidian").mkdir(parents=True)

    (root / "alpha.md").write_text(
        fm.emit({"title": "Alpha"}, "hello world\nsecond line\n"), encoding="utf-8"
    )
    (root / "beta.md").write_text(
        fm.emit({"title": "Beta Note", "tags": ["python"]}, "nothing to see\n"),
        encoding="utf-8",
    )
    (root / "Archived" / "gamma.md").write_text("hello archived\n", encoding="utf-8")
    (root / ".obsidian" / "hidden.md").write_text("hello hidden\n", encoding="utf-8")
    return root


def test_substring_matches_body_and_path(tmp_path):
    root = build_vault(tmp_path)
    paths = {r["path"] for r in search.search_notes(root, "hello")}
    assert "alpha.md" in paths
    assert "Archived/gamma.md" in paths


def test_tag_matching(tmp_path):
    root = build_vault(tmp_path)
    results = search.search_notes(root, "python")
    assert [r["path"] for r in results] == ["beta.md"]


def test_hash_prefixed_tag_matching(tmp_path):
    root = build_vault(tmp_path)
    assert [r["path"] for r in search.search_notes(root, "#python")] == ["beta.md"]


def test_internal_dirs_are_excluded(tmp_path):
    root = build_vault(tmp_path)
    paths = {r["path"] for r in search.search_notes(root, "hello")}
    assert not any(".obsidian" in p for p in paths)


def test_limit_is_respected(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for i in range(5):
        (root / f"n{i}.md").write_text("match me\n", encoding="utf-8")
    assert len(search.search_notes(root, "match", limit=2)) == 2


def test_title_prefers_frontmatter_over_filename(tmp_path):
    root = build_vault(tmp_path)
    result = next(r for r in search.search_notes(root, "beta") if r["path"] == "beta.md")
    assert result["title"] == "Beta Note"


def test_snippet_shape(tmp_path):
    root = build_vault(tmp_path)
    result = next(r for r in search.search_notes(root, "hello") if r["path"] == "alpha.md")
    assert set(result) == {"path", "title", "snippet", "updated"}
    assert isinstance(result["snippet"], str)
    assert "hello" in result["snippet"].lower()
