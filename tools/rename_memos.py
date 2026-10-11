#!/usr/bin/env python3
"""rename_memos.py (v1) - rename <uid>.md vault notes to <slug(title)>.md.

PROVENANCE
----------
Recovered 2026-10-11 from `/tmp/rename_memos.py` (5661 bytes, mtime 2026-10-10
20:22) on the NAS. Like the exporter, this lived ONLY in /tmp and would be lost
on reboot. It is the "missing" step that `export_memos.py` does not do: the
exporter writes `<uid>.md`, and THIS tool renames those to `<slug(title)>.md`,
rewrites `[[wikilinks]]`, and injects `aliases:` so old uid/name links resolve.

It is the tool that produced the 337 slug-named files in the live vault. Its
`--apply` run is evidenced by the pre-change backup it leaves behind at
`/tmp/vault-backup-20261010-202240.tar.gz` (20:22, exactly its mtime).

Durable copy: /volumeUSB1/usbshare/docker/obsidian/tools/rename_memos.py
SUPERSEDED BY `rename_memos2.py` (recursive; walks Archived/, adds fm_val()).
Keep v1 for history; use v2 going forward.

BEHAVIOUR
---------
* Dry-run by default; pass `--apply` to execute.
* On apply it FIRST tars the whole vault to /tmp/vault-backup-<ts>.tar.gz.
* Renames only files whose basename matches UID_RE (^[A-Za-z0-9]{20,}$).
* Slug rule: lower-case, non-alnum -> '-', collapse runs, cap 60 then cut at the
  last complete word; empty -> "untitled".
* Collision disambiguation appends `-<uid[:6]>`.
* Rewrites [[...]] links via a token map (uid + old name + title + slug).
* Injects the old uid/name into `aliases:` frontmatter.

NOTE: v1 is NON-recursive - it only scans the vault ROOT (os.listdir), so
`Archived/` is left untouched. That is exactly why an `Archived/` generation of
uid-named files still exists. Use v2.
"""

import os, re, json, sys, shutil, datetime, tarfile

V = "/volumeUSB1/usbshare/docker/obsidian/vault"

APPLY = "--apply" in sys.argv

TS = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

UID_RE = re.compile(r'^[A-Za-z0-9]{20,}$')


def slug(t):
    t = (t or "").strip()
    trans = {0x2014:'-', 0x2013:'-', 0x00b7:'-', 0x2192:'-', 0x2019:"", 0x2018:"",
             0x201c:'', 0x201d:'', 0x2026:'', 0x00a0:' ', ord('&'):' and '}
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


md_files = [f for f in os.listdir(V) if f.endswith(".md") and f != "_Index.md"]

# collect
entries = {}  # filename (no ext) -> dict
used = set(f[:-3] for f in md_files)  # reserve current names to avoid collisions

for f in md_files:
    name = f[:-3]
    raw = open(os.path.join(V, f), encoding="utf-8").read()
    m = re.search(r'^uid:\s*"?([^"\n]+?)"?\s*$', raw, re.M)
    uid = m.group(1) if m else ""
    t = re.search(r'^title:\s*(.*)$', raw, re.M)
    title = ""
    if t:
        try: title = json.loads(t.group(1))
        except Exception: title = t.group(1).strip().strip('"')
    entries[name] = {"uid": uid, "title": title, "raw": raw, "is_uid": bool(UID_RE.match(name))}

# decide new names
rename = {}      # old name -> new name
targets = {}     # any token (uid, old name) -> new name  for link rewrite
reserved = set() # new names we assign

for name, e in entries.items():
    if e["is_uid"]:
        base = slug(e["title"]) if e["title"] else ""
        if not base or base == "untitled":
            _body = e["raw"]
            if _body.startswith("---"):
                _end = _body.find("\n---", 3)
                if _end != -1: _body = _body[_end+4:]
            base = slug(first_line_title(_body)) or name
        new = base
        # avoid collision with existing pretty files and other new names
        n = 1
        while new in used or new in reserved:
            if new == name: break
            suf = e["uid"][:6] if e["uid"] else str(n)
            new = f"{base}-{suf}"
            n += 1
            if n > 50: break
        if new != name:
            rename[name] = new
        reserved.add(new)
    else:
        reserved.add(name)

# build token->newname map (uids AND old filenames)
for name, e in entries.items():
    new = rename.get(name, name)
    if e["uid"]:
        targets[e["uid"]] = new
    targets[name] = new
    if e["title"]:
        targets.setdefault(slug(e["title"]), new)
        targets.setdefault(e["title"], new)

# report
print(f"files: {len(entries)}  to rename (uid-named): {len(rename)}")
print("sample renames:")
for old, new in list(rename.items())[:15]:
    print(f"  {old} -> {new}")

# find collisions where final new name duplicates a kept file (shouldn't happen)
finals = {}
for name, e in entries.items():
    fn = rename.get(name, name)
    finals.setdefault(fn, []).append(name)
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


# split frontmatter / body
def split_fm(raw):
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[:end+4], raw[end+4:]
    return "", raw


def add_aliases(raw, extra):
    fm, body = split_fm(raw)
    if not fm:
        return raw
    m = re.search(r'^aliases:\s*(.*)$', fm, re.M)
    if m:
        try: cur = json.loads(m.group(1))
        except Exception: cur = []
        if not isinstance(cur, list): cur = [cur]
        for x in extra:
            if x and x not in cur: cur.append(x)
        newline = "aliases: " + json.dumps(cur, ensure_ascii=False)
        fm = fm[:m.start()] + newline + fm[m.end():]
    else:
        ins = "aliases: " + json.dumps([x for x in extra if x], ensure_ascii=False) + "\n"
        fm = fm.rstrip("\n") + "\n" + ins
    return fm + body


changed_links = 0

if APPLY:
    # backup
    bak = f"/tmp/vault-backup-{TS}.tar.gz"
    with tarfile.open(bak, "w:gz") as tar:
        tar.add(V, arcname="vault")
    print("backup:", bak)

    # write new/changed content
    for name, e in entries.items():
        old_path = os.path.join(V, name + ".md")
        new_name = rename.get(name, name)
        new_path = os.path.join(V, new_name + ".md")
        body = fix_links(e["raw"])
        body2 = add_aliases(body, [e["uid"], name if name != new_name else ""])
        # count link changes
        with open(new_path, "w", encoding="utf-8") as fh:
            fh.write(body2)
        if new_path != old_path and os.path.exists(old_path):
            os.remove(old_path)

    # rewrite _Index.md
    ip = os.path.join(V, "_Index.md")
    if os.path.exists(ip):
        it = fix_links(open(ip, encoding="utf-8").read())
        open(ip, "w", encoding="utf-8").write(it)

    print("DONE. files now:", len([f for f in os.listdir(V) if f.endswith('.md')]))
else:
    print("\n(DRY RUN - re-run with --apply to execute)")
