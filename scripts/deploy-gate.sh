#!/usr/bin/env bash
#
# deploy-gate.sh - prove a deployed notes-mcp is actually reachable and serving.
#
# MCP has the "green registration / dead server" failure mode the A2A probe
# exists to catch: mcphub can show a server as registered while nothing answers
# on the wire. This gate talks to the server directly and fails loudly.
#
# RUN THIS FROM A PEER ON THE `ai_proxy` NETWORK - never the host. The host
# cannot resolve `notes-mcp`, and `nas:8767` does not resolve from *inside* the
# bridge network. The through-the-hub rule is the whole point.
#
# Checks:
#   1. POST <url>/mcp `initialize` -> a JSON-RPC result with serverInfo.
#   2. POST <url>/mcp `tools/list` -> exactly the five notes_* tools.
#   3. GET  <mcphub>/api/servers/notes-mcp -> status: connected.
#
# Env:
#   NOTES_MCP_URL  default http://notes-mcp:8767/mcp
#   MCPHUB_URL     default http://nas:8781
#                  (the mcphub API is on 8781, NOT 3000 - see note below)
#   MCPHUB_TOKEN   optional; sent as `x-auth-token` on the hub check.
#                  The dashboard API uses `x-auth-token: <JWT>`, not
#                  `Authorization: Bearer`. Set this if check 3 returns 401.
#
# Port note: an earlier revision defaulted MCPHUB_URL to http://nas:3000.
# mcphub's API actually listens on 8781, so check 3 was silently failing
# against the wrong port. Fixed 2026-10-11.
#
set -euo pipefail

NOTES_MCP_URL="${NOTES_MCP_URL:-http://notes-mcp:8767/mcp}"
MCPHUB_URL="${MCPHUB_URL:-http://nas:8781}"
EXPECTED_TOOLS=(notes_search notes_read notes_write notes_append notes_list)

fail() {
  printf 'deploy-gate: FAIL: %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'deploy-gate: OK: %s\n' "$*"
}

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v jq >/dev/null 2>&1 || fail "jq is required"

# A JSON-RPC POST to the MCP endpoint.
rpc() {
  curl -fsS --max-time 15 -X POST "$NOTES_MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$1"
}

# Streamable-HTTP may answer as SSE ("event: message\ndata: {...}") or as plain
# JSON. Normalise both to the JSON payload so jq can read it.
extract_json() {
  local body="$1"
  if [[ "$body" == \{* ]]; then
    printf '%s' "$body"
  else
    sed -n 's/^data:[[:space:]]*//p' <<<"$body"
  fi
}

printf 'deploy-gate: probing %s\n' "$NOTES_MCP_URL"

# --- 1. initialize -----------------------------------------------------------
init_body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"notes-mcp-deploy-gate","version":"0.1.0"}}}'
init_resp="$(rpc "$init_body")" || fail "initialize request to $NOTES_MCP_URL failed"
if ! extract_json "$init_resp" | jq -e '.result.serverInfo' >/dev/null 2>&1; then
  fail "initialize returned no serverInfo (body: $init_resp)"
fi
pass "initialize returned serverInfo"

# Best-effort: a notification is harmless for a stateless server, and required
# by some clients before tools/list.
rpc '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' >/dev/null 2>&1 || true

# --- 2. tools/list -----------------------------------------------------------
tools_resp="$(rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')" \
  || fail "tools/list request failed"
names="$(extract_json "$tools_resp" | jq -r '.result.tools[].name' 2>/dev/null || true)"

[ -n "$names" ] || fail "tools/list returned no tools (body: $tools_resp)"
for tool in "${EXPECTED_TOOLS[@]}"; do
  grep -qx "$tool" <<<"$names" || fail "tool '$tool' missing (got: $(tr '\n' ' ' <<<"$names"))"
done
count="$(wc -l <<<"$names" | tr -d ' ')"
[ "$count" -eq "${#EXPECTED_TOOLS[@]}" ] \
  || fail "expected ${#EXPECTED_TOOLS[@]} tools, got $count: $(tr '\n' ' ' <<<"$names")"
pass "tools/list exposes exactly the five notes_* tools"

# --- 3. mcphub registration --------------------------------------------------
# Optional auth header (the hub API uses `x-auth-token`, not `Bearer`).
hub_hdr=()
if [ -n "${MCPHUB_TOKEN:-}" ]; then
  hub_hdr=(-H "x-auth-token: ${MCPHUB_TOKEN}")
fi
status="$(curl -fsS --max-time 15 ${hub_hdr[@]+"${hub_hdr[@]}"} \
  "$MCPHUB_URL/api/servers/notes-mcp" | jq -r '.status')" \
  || fail "GET $MCPHUB_URL/api/servers/notes-mcp failed (is mcphub on 8781? set MCPHUB_TOKEN if 401)"
[ "$status" = "connected" ] \
  || fail "mcphub reports status '$status' for notes-mcp, expected 'connected'"
pass "mcphub reports notes-mcp connected"

printf 'deploy-gate: PASS - notes-mcp is serving and registered\n'
