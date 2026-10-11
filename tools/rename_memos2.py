#!/usr/bin/env python3
"""rename_memos2.py (v2) - recursive uid -> slug vault renamer. CANONICAL.

PROVENANCE
----------
Recovered 2026-10-11 from `/tmp/rename_memos2.py` (5634 bytes, mtime 2026-10-10
21:33) on the NAS - /tmp only, lost on reboot. Evidence its `--apply` ran: the
larger recursive backup `/tmp/vault-backup-20261010-213318.tar.gz` (21:33, 4.6 MB
vs v1's 2.2 MB) written at exactly its mtime.

Durable copy: /volumeUSB1/usbshare/docker/obsidian/tools/rename_memos2.py

This is the tool that produced the vault's slug filenames. It, the exporter
(`export_memos.py`) and v1 are the three generators of the 2026-10-10 export;
all three lived only in /tmp. Now preserved here.

WHY v2 SUPERSEDES v1
--------------------
* Recursive: `os.walk` over the vault, so it also renames inside `Archived/`.
  v1 used `os.listdir` on the root only.
* SKIP_DIRS = {.obsidian, .trash, .livesync, headless-vault-livesync-v2,
  @eaDir} - internal LiveSync state is never touched.
* Adds the `fm_val()` frontmatter helper.
* Preserves each file's subdirectory (dir + "/" + new_base + ".md").

IMPORTANT - KNOWN LIMITATIONS (understand before re-running)
------------------------------------------------------------
1. `--apply` tars the whole vault to /tmp/vault-backup-<ts>.tar.gz FIRST, but
   that backup is /tmp-only: it dies with the tmpfs. Copy it off immediately.
2. Its `rename`/`targets` maps are keyed by BASENAME and UID globally, not by
   path. Two different memos sharing a title in different dirs collided here,
   which is how root and Archived/ ended up with *the same* slug filename but
   *different* content (see memos/DR6WbxM9ad3zEEk89bjhGv). Do not re-run this
   over a merged tree expecting it to be idempotent.
3. Slug is NOT sanitised to <=60 in a word-safe way for unicode titles beyond
   the translate table; long titles are hard-cut at 60 then trimmed to the last
   '-'.
4. No reverse mapping file is written, so the uid->slug table is only
   reconstructable from the `aliases:` it injected.

BEHAVIOUR
---------
* Dry-run by default; `--apply` to execute (with the backup above).
* Renames only basenames matching UID_RE = ^[A-Za-z0-9]{20,}$ (and != "_Index").
* Slug: lower-case, non-alnum -> '-', collapse, cap 60, -> "untitled" if empty.
* Collision suffix: `-<uid[:6]>`; falls back to a counter, bails after 50.
* Rewrites [[...]] links via the token map (uid, old basename, title, slug).
* Injects old uid/basename into `aliases:`.
"""

import os, re, json, sys, shutil, datetime, tarfile

V = "/volumeUSB1/usbshare/docker/obsidian/vault"

APPLY = "--apply" in sys.argv

TS = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

SKIP_DIRS = {".obsidian", ".trash", ".livesync", "headless-vault-livesync-v2", "@eaDir"}

UID_RE = re.compile(r'^[A-Za-z0-9]{20,}$')


def slug(t):
    t = (t or "").strip()
    trans = {0x2014:'-', 0x2013:'-', 0x00b7:'-', 0x2192:'-', 0x2019:"", 0x2018:"",
             0x201c:'', 0x201d:'', 0x2026:'', 0x2020:'', 0x00a0:' ', ord('&'):' and '}
    t = t.translate(trans).lower()
    t = re.sub(r'[\\/:*?"<>|#^\[\]{}()]+', ' ', t)
    t = re.sub(r'[^a-z0-9]+', '-', t)
    t = re.sub(r'-+', '-', t).strip('-')
    if len(t) > 60:
        t = t[:60]
        if '-' in t:
            t = t.rsplit('-', 1)[0]
    return t.strip('-') or "untitled"


def first_line_title(content):
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            line = line.lstrip('#').strip()
        if line:
            return line[:80]
    return ""


def split_fm(raw):
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[:end+4], raw[end+4:]
    return "", raw


def fm_val(raw, key):
    m = re.search(r'^' + key + r':\s*(.*)$', raw, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    try:
        return json.loads(v)
    except Exception:
        return v.strip().strip('"')


def add_aliases(raw, extra):
    fm, body = split_fm(raw)
    if not fm:
        return raw
    m = re.search(r'^aliases:\s*(.*)$', fm, re.M)
    if m:
        try:
            cur = json.loads(m.group(1))
        except Exception:
            cur = []
        if not isinstance(cur, list):
            cur = [cur]
        for x in extra:
            if x and x not in cur:
                cur.append(x)
        newline = "aliases: " + json.dumps(cur, ensure_ascii=False)
        fm = fm[:m.start()] + newline + fm[m.end():]
    else:
        ins = "aliases: " + json.dumps([x for x in extra if x], ensure_ascii=False) + "\n"
        fm = fm.rstrip("\n") + "\n" + ins
    return fm + body


# ── collect all md files (recursive, skipping internal dirs) ──
entries = {}  # relpath -> dict

for root, dirs, files in os.walk(V):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        if not f.endswith(".md"):
            continue
        rel = os.path.relpath(os.path.join(root, f), V).replace("\\", "/")
        raw = open(os.path.join(root, f), encoding="utf-8").read()
        base = f[:-3]
        entries[rel] = {
            "dir": os.path.dirname(rel),
            "base": base,
            "raw": raw,
            "uid": fm_val(raw, "uid"),
            "title": fm_val(raw, "title") or "",
            "is_uid": bool(UID_RE.match(base)) and base != "_Index",
        }

# existing basenames reserved (avoid collisions)
used = set(e["base"] for e in entries.values())

rename = {}      # base (old) -> base (new)
reserved = set()

for rel, e in sorted(entries.items()):
    if not e["is_uid"]:
        reserved.add(e["base"])
        continue
    base = slug(e["title"]) if e["title"] else ""
    if not base or base == "untitled":
        _, body = split_fm(e["raw"])
        base = slug(first_line_title(body)) or e["base"]
    new = base
    n = 1
    while new in used or new in reserved:
        if new == e["base"]:
            break
        suffix = (e["uid"][:6] if e["uid"] else str(n))
        new = f"{base}-{suffix}"
        n += 1
        if n > 50:
            break
    if new != e["base"]:
        rename[e["base"]] = new
    reserved.add(new)

# token -> new basename (links use basenames in Obsidian)
targets = {}
for rel, e in entries.items():
    new = rename.get(e["base"], e["base"])
    if e["uid"]:
        targets[e["uid"]] = new
    targets[e["base"]] = new
    if e["title"]:
        targets.setdefault(slug(e["title"]), new)
        targets.setdefault(e["title"], new)


print(f"md files: {len(entries)}  uid-named to rename: {len(rename)}")
print("sample:")
for o, n in list(rename.items())[:12]:
    print(f"  {o} -> {n}")

finals = {}
for rel, e in entries.items():
    fn = rename.get(e["base"], e["base"])
    finals.setdefault(fn, []).append(rel)
dups = {k: v for k, v in finals.items() if len(v) > 1}
print("final-name collisions:", dups)


link_re = re.compile(r'\[\[([^\]|#]*)((?:#[^\]|]*)?)(\|[^\]]*)?\]\]')

def fix_links(text):
    def rep(m):
        tgt = m.group(1).strip()
        new = targets.get(tgt)
        if new and new != tgt:
            return f"[[{new}{m.group(2) or ''}{m.group(3) or ''}]]"
        return m.group(0)
    return link_re.sub(rep, text)


if APPLY:
    bak = f"/tmp/vault-backup-{TS}.tar.gz"
    with tarfile.open(bak, "w:gz") as tar:
        tar.add(V, arcname="vault")
    print("backup:", bak)

    for rel, e in entries.items():
        new_base = rename.get(e["base"], e["base"])
        d = e["dir"]
        old_path = os.path.join(V, rel)
        new_rel = (d + "/" + new_base + ".md") if d else (new_base + ".md")
        new_path = os.path.join(V, new_rel)
        body = fix_links(e["raw"])
        extra = [x for x in (e["uid"], e["base"] if e["base"] != new_base else "") if x]
        out = add_aliases(body, extra)
        with open(new_path, "w", encoding="utf-8") as fh:
            fh.write(out)
        if os.path.abspath(new_path) != os.path.abspath(old_path):
            os.remove(old_path)

    n = sum(1 for r, _, fs in os.walk(V) if False)
    print("DONE. renamed:", len(rename))
else:
    print("\n(DRY RUN - re-run with --apply)")
