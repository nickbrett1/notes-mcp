#!/usr/bin/env bash
#
# check-vault-mount.sh - catch a stale /vault bind mount on the notes-mcp host.
#
# WHY THIS EXISTS
#   Docker bind mounts resolve the *source path* to a directory inode when the
#   container is created, and keep that inode for the container's life. If the
#   vault directory is later renamed/recreated on the host (e.g. `vault` ->
#   `vault-old`, then a fresh `vault`), the running container silently keeps
#   reading the OLD directory. The serve looks healthy - /healthz answers, notes
#   list - but it is the wrong vault, so notes the user creates on the iPhone
#   (which syncs the live directory) never appear.
#
#   Observed 2026-10-12: notes-mcp's /vault held inode 11669587 (`vault-old`)
#   while the host `vault` was 11796567; two freshly-synced notes were invisible
#   to every notes_* tool. A `docker compose up -d --force-recreate` fixed it.
#
# WHAT IT CHECKS
#   Compares the inode of the host vault path with the inode of /vault as the
#   container actually sees it (`docker exec ... stat -c %i /vault`, so it needs
#   neither root nor access to /proc/<pid>/root). Equal => the mount is current.
#   Different => the mount is stale; recreate the container.
#
# RUN THIS ON THE HOST THAT RUNS notes-mcp (currently ryzen), not a peer - it
# needs the host path and the docker socket. Suitable for cron / Dagu / a
# Homepage monitor.
#
# ENV:
#   HOST_VAULT   default /srv/docker/obsidian/vault
#   CONTAINER    default notes-mcp
#   DOCKER       default docker   (e.g. DOCKER="sudo docker")
#
set -euo pipefail

HOST_VAULT="${HOST_VAULT:-/srv/docker/obsidian/vault}"
CONTAINER="${CONTAINER:-notes-mcp}"
DOCKER="${DOCKER:-docker}"

fail() { printf 'check-vault-mount: FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'check-vault-mount: OK: %s\n' "$*"; }

[ -d "$HOST_VAULT" ] || fail "host vault '$HOST_VAULT' is not a directory"

running="$($DOCKER inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" \
  || fail "cannot inspect container '$CONTAINER' (need docker access; try DOCKER='sudo docker')"
[ "$running" = "true" ] || fail "container '$CONTAINER' is not running (running=$running)"

host_ino="$(stat -c '%i' "$HOST_VAULT")" || fail "cannot stat host vault"
container_ino="$($DOCKER exec "$CONTAINER" stat -c '%i' /vault 2>/dev/null)" \
  || fail "cannot stat /vault inside container '$CONTAINER'"

printf 'check-vault-mount: host %s inode=%s · container /vault inode=%s\n' \
  "$HOST_VAULT" "$host_ino" "$container_ino"

if [ "$host_ino" != "$container_ino" ]; then
  fail "STALE /vault BIND MOUNT: container '$CONTAINER' is reading a different directory
       than the host path (the vault was likely renamed/recreated after the container started).
       Notes created in the live vault are invisible to every notes_* tool.
       Fix:  cd <notes-mcp compose dir> && $DOCKER compose up -d --force-recreate <service>"
fi

pass "container '$CONTAINER' /vault matches the host vault (inode $host_ino)"
