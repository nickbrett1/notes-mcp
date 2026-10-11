#!/usr/bin/env python3
"""Export Memos -> Obsidian vault (one .md per memo + _Index.md).

PROVENANCE
----------
Recovered 2026-10-11. This script previously existed ONLY at
`/tmp/export_memos.py` on the NAS (3519 bytes, mtime 2026-10-10 17:52) - i.e.
in a tmpfs that is lost on reboot. It is the tool that produced the 2026-10-10
vault export (~667 notes), so it is the single most valuable artifact of the
Memos->Obsidian migration and must not live only in /tmp.

The authoritative byte-identical copy now lives at:
    /volumeUSB1/usbshare/docker/obsidian/tools/export_memos.py
(`cmp` verified identical to /tmp/export_memos.py at recovery time). This repo
copy is the durable, versioned one; keep the two in sync.

KNOWN LIMITATIONS (deliberately NOT fixed here - change under review)
--------------------------------------------------------------------
* Reads the DB with `mode=ro`, which does NOT fold in the -wal file. Memos had
  a ~3.4 MB un-checkpointed WAL (~45% of live data) at recovery time, so this
  script could export a STALE snapshot. Prefer `VACUUM INTO` a temp file and
  read that, or checkpoint first.
* Numeric "tags" from an Apple-Notes import are dropped (see the fullmatch
  filter); real tags survive.
* Relations are rendered as `[[wikilink]]` sections; there is no attempt to
  inline comments as callouts.
"""

import sqlite3, json, os, re, datetime

DB = "/volumeUSB1/usbshare/docker/memos/data/memos_prod.db"
OUT = "/volumeUSB1/usbshare/docker/obsidian/vault"

os.makedirs(OUT, exist_ok=True)

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()

rows = c.execute(
    "select id,uid,content,created_ts,updated_ts,row_status,visibility,pinned,payload from memo"
).fetchall()
id2uid = {r["id"]: r["uid"] for r in rows}

rel = c.execute("select memo_id, related_memo_id, type from memo_relation").fetchall()
out_ref, in_comment, comment_on = {}, {}, {}
for r in rel:
    mid, rid, t = r["memo_id"], r["related_memo_id"], r["type"]
    if t == "REFERENCE":
        out_ref.setdefault(mid, []).append(id2uid.get(rid, ""))
    elif t == "COMMENT":
        in_comment.setdefault(rid, []).append(id2uid.get(mid, ""))
        comment_on.setdefault(mid, []).append(id2uid.get(rid, ""))


def ts(v):
    return datetime.datetime.fromtimestamp(v, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe(name):
    return (re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "untitled")


used, index, count = set(), [], 0
for r in rows:
    uid = r["uid"]
    try:
        payload = json.loads(r["payload"] or "{}")
    except Exception:
        payload = {}
    props = payload.get("property", {}) if isinstance(payload, dict) else {}
    title = props.get("title") or ""
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    # Memos stored junk numeric "tags"; drop pure digit/slash entries
    tags = [str(t) for t in tags if not re.fullmatch(r"[0-9/]+", str(t))]

    fm = ["---", f'uid: "{uid}"']
    if title:
        fm.append(f"title: {json.dumps(title)}")
    fm += [
        f'created: {ts(r["created_ts"])}',
        f'updated: {ts(r["updated_ts"])}',
        f'status: {r["row_status"]}',
        f'visibility: {r["visibility"]}',
    ]
    if r["pinned"]:
        fm.append("pinned: true")
    if tags:
        fm.append("tags: [" + ", ".join(json.dumps(t) for t in tags) + "]")
    if title:
        fm.append("aliases: [" + json.dumps(title) + "]")
    fm.append("---")

    parts = ["\n".join(fm), "", r["content"] or ""]
    refs = [x for x in out_ref.get(r["id"], []) if x]
    if refs:
        parts.append("\n## Related\n" + "\n".join(f"- [[{x}]]" for x in refs))
    if r["id"] in comment_on:
        parts.append("\n## Comment on\n" + "\n".join(f"- [[{x}]]" for x in comment_on[r["id"]] if x))
    cmts = [x for x in in_comment.get(r["id"], []) if x]
    if cmts:
        parts.append("\n## Comments\n" + "\n".join(f"- [[{x}]]" for x in cmts))

    fname = safe(uid)
    while fname in used:
        fname += "_"
    used.add(fname)
    with open(os.path.join(OUT, fname + ".md"), "w") as f:
        f.write("\n".join(parts).rstrip() + "\n")
    count += 1
    index.append((r["row_status"], ts(r["created_ts"])[:10], fname, title))

index.sort(key=lambda x: (x[0] != "NORMAL", x[1]))
with open(os.path.join(OUT, "_Index.md"), "w") as f:
    f.write("# Memos Export Index\n\n")
    for status in ("NORMAL", "ARCHIVED"):
        grp = [i for i in index if i[0] == status]
        f.write(f"## {status} ({len(grp)})\n\n")
        for st, d, fn, t in grp:
            f.write(f"- {d} — [[{fn}]]" + (f" — {t}" if t else "") + "\n")
        f.write("\n")

print("exported", count, "memos ->", OUT)
print("REFERENCE links:", sum(len(v) for v in out_ref.values()),
      "| COMMENT links:", sum(len(v) for v in in_comment.values()))
