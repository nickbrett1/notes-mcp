"""The MCP server: the five tools plus the ``/healthz`` route.

Per the design rule, every tool body is a thin marshal over a plain function in
:mod:`notes_mcp.vault` / :mod:`notes_mcp.search` - nothing here touches the
filesystem itself.
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, search, vault
from . import frontmatter as fm

mcp = MCPServer(
    name="notes-mcp",
    version=__version__,
    instructions=(
        "Read and write markdown notes in the Obsidian vault. Paths are "
        "vault-relative; absolute paths and paths that escape the vault are "
        "refused. One note per call. A note's full text is capped (default "
        "51,200 characters, configurable via NOTES_MAX_CHARS); a write or "
        "append that would exceed the cap is refused — split into smaller notes."
    ),
)


@mcp.tool()
def notes_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search notes by case-insensitive substring (path, title, body) or tag."""
    return search.search_notes(vault.vault_root(), query, limit=limit)


@mcp.tool()
def notes_read(path: str) -> str:
    """Read one note's full text (frontmatter + body) by vault-relative path."""
    return vault.read_note(vault.vault_root(), path)


@mcp.tool()
def notes_write(
    path: str | None = None,
    title: str = "",
    body: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Create or overwrite one note.

    If ``path`` is omitted it is derived from ``title`` as ``<slug>.md`` at the
    vault root. ``created`` and ``aliases`` are preserved on overwrite. The
    whole note (frontmatter + body) is capped (default 51,200 chars); a larger
    write is refused, so keep notes focused and split when needed.
    """
    rel = path if path else f"{fm.slug(title) or 'untitled'}.md"
    return vault.write_note(
        vault.vault_root(), rel, title=title, body=body, tags=tags
    )


@mcp.tool()
def notes_append(path: str, text: str) -> dict[str, Any]:
    """Append ``text`` to an existing note as a new paragraph.

    The resulting note (frontmatter + body) is capped (default 51,200 chars);
    an append that would exceed the cap is refused.
    """
    return vault.append_note(vault.vault_root(), path, text)


@mcp.tool()
def notes_list(
    folder: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List notes as ``{path, title, updated}``; ``folder`` narrows the browse."""
    return vault.list_notes(vault.vault_root(), folder=folder, limit=limit)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    """Liveness/readiness probe used by the container healthcheck."""
    root = vault.vault_root()
    return JSONResponse(
        {"status": "ok", "vault": str(root), "notes": vault.count_notes(root)}
    )
