# 001 — Implement the notes-mcp MCP server

**Status:** ready to build. **Owner:** the repo's `notes-mcp-dev` agent.
**Design source:** `memos/oT7bLoN3RFbjQCt7CF4Xhb` (NAS Memos) — the memo is
authoritative on *why*; this spec is authoritative on *what*.

Deliverable: a working, tested MCP server in `src/notes_mcp/` that exposes the
Obsidian vault over stdio and streamable-http, plus the tests in §8 green.

---

## 0. Design rule

The MCP server is a **thin skin over plain functions**. The vault operations are
the product; the `@mcp.tool` bodies only marshal arguments and hand back dicts.
Nothing else touches the filesystem. `src/notes_mcp/vault.py` is the **only**
module that does path work, so the whole security surface is one reviewable file.

---

## 1. Package layout to create

```
src/notes_mcp/
  __init__.py     # already exists: __version__ = "0.1.0" - keep
  __main__.py     # argparse + transport selection (stdio | streamable-http)
  vault.py        # THE only module that touches the vault filesystem
  frontmatter.py  # parse/emit YAML frontmatter; slug; created/updated/aliases
  search.py       # substring + tag matching; snippet extraction
  server.py       # MCPServer(...) + the 5 @mcp.tool bodies + /healthz route
```

`pyproject.toml` already declares `mcp>=2` and `pyyaml>=6` as dependencies.

---

## 2. Tool surface (exactly five tools)

Naming: keep the `notes_` prefix (groups cleanly in mcphub/tool listings).

| Tool | Input | Output | Notes |
|---|---|---|---|
| `notes_search` | `query: str`, `limit: int = 10` | list of `{path, title, snippet, updated}` | case-insensitive substring over filename + body, plus `#tag`/`tags:` matching |
| `notes_read` | `path: str` | full note text (frontmatter + body) | exact read by path |
| `notes_write` | `path: str \| None`, `title: str`, `body: str`, `tags: list[str] = []` | `{path, created_or_updated}` | create-or-overwrite; if `path` omitted, derive `slug(title) + ".md"` at vault root |
| `notes_append` | `path: str`, `text: str` | `{path, updated}` | safe additive edit |
| `notes_list` | `folder: str \| None`, `limit: int = 50` | list of `{path, title, updated}` | browse; includes `Archived/` |

**No `notes_delete`.** Deletion is the highest-risk op (a LiveSync tombstone
pushed to every device). Ship without it; archive-over-delete is the intended
eventual shape. Do not add it speculatively.

**Bulk writes are forbidden by design.** One file per call — LiveSync's watcher
can miss large batches. `notes_write` must never loop over multiple notes.

**Note-size ceiling.** A note's full text (frontmatter + body) is capped at
51,200 characters, mirroring the ceiling of the Memos system this replaces. The
point is to stop an agent writing far more in a single note than it needs to,
which bloats the context of every later reader. `notes_write` and `notes_append`
enforce the cap on the **rendered** text and raise `NoteTooLargeError` (the write
is refused, nothing is written) when it would be exceeded; the message tells the
caller to split the content into smaller notes. The limit is configurable via the
`NOTES_MAX_CHARS` env var; an unset or non-positive/non-integer value falls back
to the 51,200 default so a bad value can never disable the guard.

---

## 3. Path safety — `vault.py` (the crown jewel)

```python
INTERNAL_DIRS = {"headless-vault-livesync-v2", ".livesync", ".trash", ".obsidian"}

def resolve_note(root: Path, rel: str, *, write: bool = False) -> Path:
    if rel.startswith("/"):
        raise VaultPathError("absolute paths are not allowed")
    p = (root / rel).resolve()          # resolves .. and symlinks
    p.relative_to(root.resolve())       # raises ValueError if it escapes
    if any(part in INTERNAL_DIRS for part in p.parts):
        raise VaultPathError("path is inside an internal directory")
    if any(part.endswith("@eaDir") for part in p.parts):
        raise VaultPathError("synology @eaDir is not a note")
    return p
```

- Vault root comes from env `NOTES_VAULT` (default `/vault`).
- Reject a target that is an existing directory.
- Skip `@eaDir` on **read walks** as well as rejecting it in `resolve_note`.
- `vault.py` owns: root resolution, `resolve_note`, read / write / append / list,
  and the internal-dir/`@eaDir` filters. Nothing else touches `Path` on the vault.

---

## 4. Frontmatter + slug — `frontmatter.py`

- Round-trip: `parse(text) -> (meta: dict, body: str)`, `emit(meta, body) -> text`.
- `slug(title)`: lowercase; non-alnum → `-`; collapse runs; strip; **cap 60 chars**.
- **Preserve `created` and `aliases` on overwrite**; always bump `updated` to now
  (UTC ISO-8601). Emit `title` and `tags` from the tool args.
- Keep old Memos UIDs in `aliases:` — the phase-5 migration export reconciles on them.
- Missing frontmatter on read: return the body as-is, `title` = filename stem.

---

## 5. Search — `search.py`

Deliberately plain for this phase (**not** semantic — that is an open question in
the design memo §7):

- Case-insensitive substring over filename and body, plus `#tag` / `tags:` matching.
- `snippet` = the first matching line plus a little context.
- Walk the vault but **skip `INTERNAL_DIRS` and `@eaDir`**.
- Prefer frontmatter `title` over the filename when present.
- `limit` default 10 for search, 50 for list.

---

## 6. Transport, health, auth — `server.py` + `__main__.py`

- Construct with mcp **2.3.0**'s `MCPServer` (the FastMCP→MCPServer rename is why
  the pin is `mcp>=2`).
- **Default stdio:** `python -m notes_mcp` → a local client can spawn it and
  inherit the caller's filesystem.
- **streamable-http** for mcphub:
  `python -m notes_mcp --transport streamable-http --host 0.0.0.0 --port 8767`,
  path `/mcp`, **`stateless_http=True`** (each POST stands alone; no session
  pinning). Verified present in mcp 2.3.0.
- **Bind `0.0.0.0`, not `127.0.0.1`** — the dialer is mcphub across `ai_proxy`.
- **`/healthz`** via `@mcp.custom_route("/healthz", methods=["GET"])` (confirmed in
  mcp 2.3.0), returning `{"status":"ok","vault":"<root>","notes":<count>}`.
  The Docker healthcheck probes `http://127.0.0.1:8767/healthz`.
- **Auth: NONE.** No `NOTES_TOKEN`, no bearer header, no auth middleware.
  `notes-mcp` is the user's own code on the user's own `ai_proxy` bridge,
  reachable only from peers on that network. The old `MEMOS_TOKEN` was a property
  of the *Memos product*, not of MCP. **Do not add auth** — decided 2026-10-11.

---

## 7. Packaging / entry point notes

- The Dockerfile's `CMD` is `["python","-m","notes_mcp"]` → **`__main__.py` must
  exist** and work with no args (stdio default) and with the flags above.
- `pyproject.toml` regenerates on `overwrite: true`; app code under `src/` survives.
- Do **not** edit `docker-compose.yml` or `Dockerfile`. The vault mount
  (`/volumeUSB1/usbshare/docker/obsidian/vault` → `/vault`, read-only) is already
  correct for the read-only phase.

---

## 8. Test plan (pytest) — the tests ARE the deliverable

Mirror the `data-sourcing-agent` style. Files under `tests/`:

- `test_vault_safety.py` — **highest value.** A table of traversal cases:
  `../`, `..%2f`, absolute paths, symlink-escaping-out, internal-dir targets,
  `@eaDir` targets — each asserting `VaultPathError`. This is the analogue of
  dsa's protocol-sensitive tests.
- `test_frontmatter.py` — slug cases; round-trip parse/emit; assert `created` and
  `aliases` survive an overwrite and `updated` is bumped.
- `test_search.py` — substring, tag, `limit`, snippet shape; assert internal dirs
  are excluded from results.
- `test_tools.py` — the no-drift rule: call each tool body directly and assert it
  equals the underlying plain-function output.
- `test_transport.py` — `main(["--transport","streamable-http"])` wires `/mcp`
  and `/healthz`; the default (no args) selects stdio.

`ruff check src tests` and `pytest` must both pass; Buildkite runs them. The
existing `tests/test_smoke.py` (package imports) stays.

---

## 9. Deploy + verify gate (after tests are green)

MCP has the same "green registration / dead server" failure mode the A2A probe
exists to catch. Ship `scripts/deploy-gate.sh`:

1. Run **from a peer on `ai_proxy`** (never the host): `POST
   http://notes-mcp:8767/mcp` `initialize` + `tools/list`; assert the 5 tool
   names, fail the deploy otherwise.
2. `GET /api/servers/notes-mcp` on mcphub → assert `status: connected`.

The "through the hub, not from the host" rule is the whole point — `nas:8767`
does not resolve from inside the bridge network.

mcphub registration (manual, API step, same flow as `data-sourcing-gate-grader`);
login → `POST /api/servers` `{type:"streamable-http", url:"http://notes-mcp:8767/mcp"}`
→ `POST /api/groups/<group>/servers`. **No `headers` needed** (§6). Register under
`notes-mcp`; add to the `data` group and any other group agents consume.

---

## 10. Definition of done

- [ ] `src/notes_mcp/` implements the 5 tools per §2, laid out per §1.
- [ ] Path safety per §3, with the §8 traversal table green.
- [ ] Note-size ceiling per §2 enforced on write/append, `tests/test_limits.py` green.
- [ ] `/healthz` returns the §6 shape; container healthcheck passes locally.
- [ ] `ruff check src tests` and `pytest` green; Buildkite green.
- [ ] stdio default and streamable-http both wire up (§8 `test_transport.py`).
- [ ] No auth added anywhere.
- [ ] `scripts/deploy-gate.sh` present (§9) — it runs in CI/on the peer, not here.

## 11. Explicitly out of scope

- `notes_delete` / `notes_archive` (deferred; see §2).
- Semantic/vector search (§5) — plain substring only for now.
- The Memos→vault migration export (a later, separate phase).
- mcphub registration itself (a manual API step, §9) — but DO ship the gate script.
- Any auth mechanism (§6).
