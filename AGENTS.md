# Agent notes — notes-mcp

Orientation for any coding agent working in this repository (the in-repo
`notes-mcp-dev` a2a-goose agent, goose in the devcontainer, or a human).

## What this repo is

An **MCP server** that gives agents a small, constrained interface to the
Obsidian vault on the NAS (read/write markdown notes), replacing the ad-hoc
`memos` MCP for note-taking. genproj generated the infra shell; the server
itself is hand-written and **not yet implemented**.

## Start here

**[`specs/001-notes-mcp-server.md`](specs/001-notes-mcp-server.md)** is the
complete, decision-locked build brief. It supersedes nothing and omits nothing;
read it in full before writing code. It carries:

- the target tool surface (5 tools, exact names and shapes),
- the package layout to create under `src/notes_mcp/`,
- path-safety rules, frontmatter/slug rules, search rules,
- the transport/health/auth decisions (auth: **none**),
- the test plan, and
- the deploy + verify gate.

The design rationale behind it lives in the memo **`memos/oT7bLoN3RFbjQCt7CF4Xhb`**
("Notes MCP — Design & Migration Plan", on the NAS Memos instance). The spec is
self-contained; the memo is background and is the place to check *why* a decision
was made.

## Current state

- **Done:** repo generated; infra correct — Dockerfile (`CMD python -m notes_mcp`),
  compose (port 8767, vault bind-mounted read-only at `/vault`, `ai_proxy`
  network), Buildkite pipeline, ruff, Doppler, devcontainer.
- **Not done:** everything under `src/notes_mcp/` beyond a version stub, and the
  tests. `Dockerfile` publishes a LAN port and the compose healthcheck probes
  `http://127.0.0.1:8767/healthz` — **the image will restart-loop until `/healthz`
  and the entry point exist**, so the smoke-test pipeline step fails until then.
  That is expected, not a bug to work around.

## Working rules

- Follow [`.agents/.rules/git_guidelines.md`](.agents/.rules/git_guidelines.md)
  and [`.agents/.rules/testing_guidelines.md`](.agents/.rules/testing_guidelines.md).
- `ruff check src tests` and `pytest` must pass; Buildkite runs both.
- Do not hand-edit generated infra files (`docker-compose.yml`, `Dockerfile`,
  `.buildkite/`, `.devcontainer/`) unless the spec says to. Regeneration with
  `overwrite: true` rewrites them; app code under `src/` survives.
- No auth, no `NOTES_TOKEN` — do not add one (spec §6).
