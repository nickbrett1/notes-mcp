#!/usr/bin/env python3
"""rebuild_vault.py - regenerate the Obsidian vault from a FROZEN Memos DB.

TARGET LAYOUT (decided 2026-10-11)
----------------------------------
Memos `row_status` maps to location, so the vault MIRRORS Memos:
    NORMAL   -> vault root          (31 notes at freeze)
    ARCHIVED -> vault/Archived/     (315 notes at freeze)
One note per memo, slug-named, uid kept in `aliases:`.

WHY THIS EXISTS
---------------
The 2026-10-10 vault was produced by /tmp/export_memos.py + /tmp/rename_memos*.py.
It ended up DOUBLE-STORED - every memo written once to root and again to
Archived/ with an IDENTICAL uid - and neither folder mirrored row_status
(root: 310 ARCHIVED + 24 NORMAL; Archived/: 311 ARCHIVED + 17 NORMAL). 663 files
for 346 memos. Rather than reconcile two bad generations by hand, regenerate
from the DB, which is the source of truth.

SAFETY
------
* Reads a SNAPSHOT, never the live DB. Feed it a `VACUUM INTO` copy, e.g.
      sqlite3 memos_prod.db "VACUUM INTO '/tmp/memos-frozen.db'"
  (Memos was frozen 2026-10-11 so the WAL is already checkpointed; still prefer
  a snapshot so a restart can never race the export.)
* Writes ONLY to --out (default a *staging* sibling dir), NEVER the live vault.
* Requires --apply to write anything; otherwise it is a dry run.
* Deletes nothing anywhere.

RENDERING RULES (hardened after a staging validation, 2026-10-11)
-----------------------------------------------------------------
* Embedded frontmatter in the memo BODY is MERGED, not duplicated: if a memo's
  content already begins with a `---` YAML block, its `title`/`tags` are lifted
  into the generated frontmatter and the inner block is removed from the body.
* Comments are rendered as collapsed Obsidian callouts with a DATE label only
  (`> [!note]- Comment (2026-08-17)`), never the body's first line, so the label
  does not repeat the opening sentence.
* Link rewriting covers BOTH `[[wikilink]]` syntax AND prose references of the
  form `memos/<uid>` / bare `<uid>`, mapping each known uid to its slug.
  (Not code-block aware - a uid quoted verbatim inside a fenced code sample will
  also be rewritten. Acceptable for this corpus; note it if the corpus changes.)
* YAML is emitted with ensure_ascii=False so `->` stays `->`, not `\u2192`.

STILL REQUIRED BEFORE A LIVE SWAP
---------------------------------
A bulk write into the live vault pushes through LiveSync to Mac + iPhone and a
DELETE is a tombstone. So: build staging -> diff -> review -> swap -> restart
`livesync`. Never point this at the live vault directly.
"""

import argparse
import datetime
import json
import os
import re
import sqlite3

DEFAULT_DB = "/volumeUSB1/usbshare/docker/memos/data/memos_prod.db"
DEFAULT_OUT = "/volumeUSB1/usbshare/docker/obsidian/vault-staging"

INTERNAL = {".obsidian", ".trash", ".livesync", "headless-vault-livesync-v2", "@eaDir"}

UID_RE = re.compile(r"^[A-Za-z0-9]{20,}$")

# --- slug: the SAME rule recovered from /tmp/rename_memos2.py (keep in sync) ---
_TRANS = {
    0x2014: "-", 0x2013: "-", 0x00b7: "-", 0x2192: "-",
    0x2019: "", 0x2018: "", 0x201c: "", 0x201d: "",
    0x2026: "", 0x2020: "", 0x00a0: " ", ord("&"): " and ",
}


def slug(t: str) -> str:
    t = (t or "").strip().translate(_TRANS).lower()
    t = re.sub(r'[\\/:*?"<>|#^\[\]{}()]+', " ", t)
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    if len(t) > 60:
        t = t[:60]
        if "-" in t:
            t = t.rsplit("-", 1)[0]
    return t.strip("-") or "untitled"


def split_embedded_fm(content: str):
    """If content starts with its own YAML frontmatter, return (meta, body).

    meta is a small dict with 'title' (str) and 'tags' (list). The block is
    REMOVED from the body so we never emit two frontmatter blocks.
    """
    if not content or not content.lstrip().startswith("---"):
        return {}, content
    body = content.lstrip()
    end = body.find("\n---", 3)
    if end == -1:
        return {}, content
    block = body[3:end]
    rest = body[end + 4:]
    meta = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = re.match(r"^(\w+)\s*:\s*(.*)$", ln)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key == "tags":
                tags = []
                if val.startswith("[") and val.endswith("]"):
                    tags = [x.strip().strip('"\'') for x in val[1:-1].split(",") if x.strip()]
                elif val:
                    tags = [val.strip('"\'')]
                else:
                    j = i + 1
                    while j < len(lines) and re.match(r"^\s*-\s+", lines[j]):
                        tags.append(lines[j].split("-", 1)[1].strip().strip('"\''))
                        j += 1
                    i = j - 1
                meta["tags"] = [t for t in tags if t]
            elif key in ("title", "aliases"):
                if val.startswith("[") and val.endswith("]"):
                    items = [x.strip().strip('"\'') for x in val[1:-1].split(",") if x.strip()]
                elif val:
                    items = [val.strip('"\'')]
                else:
                    items, j = [], i + 1
                    while j < len(lines) and re.match(r"^\s*-\s+", lines[j]):
                        items.append(lines[j].split("-", 1)[1].strip().strip('"\''))
                        j += 1
                    i = j - 1
                meta.setdefault("aliases", []).extend(items) if key == "aliases" else None
                if key == "title" and items:
                    meta["title"] = items[0]
        i += 1
    return meta, rest


def first_line_title(content: str) -> str:
    for line in (content or "").splitlines():
        s = line.strip()
        if not s or s == "---" or s.startswith("#"):
            s = s.lstrip("#").strip()
        if s and s != "---":
            return s[:80]
    return ""


def iso(ts):
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    c = con.cursor()
    rows = c.execute(
        "select id,uid,content,created_ts,updated_ts,row_status,visibility,"
        "pinned,payload from memo"
    ).fetchall()
    rel = c.execute(
        "select memo_id, related_memo_id, type from memo_relation"
    ).fetchall()
    con.close()

    memos = {}
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        props = payload.get("property", {}) if isinstance(payload, dict) else {}
        tags = payload.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        tags = [str(t) for t in tags if not re.fullmatch(r"[0-9/]+", str(t))]

        raw = r["content"] or ""
        inner, body = split_embedded_fm(raw)
        title = (props.get("title") or "").strip() or inner.get("title", "").strip()
        for t in inner.get("tags", []):
            if t and t not in tags:
                tags.append(t)

        memos[r["id"]] = {
            "id": r["id"],
            "uid": r["uid"],
            "content": body,
            "created": iso(r["created_ts"]),
            "updated": iso(r["updated_ts"]),
            "status": r["row_status"] or "NORMAL",
            "visibility": r["visibility"] or "PRIVATE",
            "pinned": bool(r["pinned"]),
            "title": title,
            "tags": tags,
            "inner_aliases": inner.get("aliases", []),
        }
    id2uid = {m["id"]: m["uid"] for m in memos.values()}

    out_ref, comments_on = {}, {}
    for r in rel:
        mid, rid, t = r["memo_id"], r["related_memo_id"], r["type"]
        if t == "REFERENCE":
            out_ref.setdefault(mid, []).append(rid)
        elif t == "COMMENT":
            comments_on.setdefault(rid, []).append(mid)
    return memos, id2uid, out_ref, comments_on


def plan_names(memos):
    """Assign each memo a (dir, slug) - collisions resolved PER TARGET DIR."""
    used, plan = {}, {}
    for m in sorted(memos.values(), key=lambda x: (x["status"] != "NORMAL", x["created"])):
        d = "Archived" if m["status"] == "ARCHIVED" else ""
        base = slug(m["title"]) if m["title"] else slug(first_line_title(m["content"]))
        if not base or base == "untitled":
            base = slug(m["uid"]) or "untitled"
        taken = used.setdefault(d, set())
        new = base
        if new in taken:
            new = f"{base}-{(m['uid'] or str(m['id']))[:6]}"
        n = 1
        while new in taken:
            n += 1
            new = f"{base}-{(m['uid'] or str(m['id']))[:6]}-{n}"
        taken.add(new)
        plan[m["id"]] = (d, new)
    return plan


def rewrite_all(text, uid2name):
    """Rewrite [[uid]] wikilinks AND prose 'memos/<uid>' / bare <uid> refs."""
    if not text:
        return text

    def rep_wiki(m):
        tgt = m.group(1).strip()
        new = uid2name.get(tgt)
        if new and new != tgt:
            return f"[[{new}{m.group(2) or ''}{m.group(3) or ''}]]"
        return m.group(0)

    text = re.sub(r"\[\[([^\]|#]*)((?:#[^\]|]*)?)(\|[^\]]*)?\]\]", rep_wiki, text)

    # prose: memos/<uid>
    text = re.sub(
        r"\bmemos/([A-Za-z0-9]{20,})\b",
        lambda m: f"[[{uid2name[m.group(1)]}]]" if m.group(1) in uid2name else m.group(0),
        text,
    )
    # prose: a bare uid token
    text = re.sub(
        r"(?<![A-Za-z0-9])([A-Za-z0-9]{20,})(?![A-Za-z0-9])",
        lambda m: f"[[{uid2name[m.group(1)]}]]" if m.group(1) in uid2name else m.group(0),
        text,
    )
    return text


def callouts(comment_ids, memos, uid2name):
    out = []
    for cid in comment_ids:
        cm = memos.get(cid)
        if not cm:
            continue
        body = rewrite_all(cm["content"].rstrip(), uid2name)
        label = f"Comment ({cm['created'][:10]})"
        lined = ["> " + ln if ln.strip() else ">" for ln in body.splitlines()]
        out.append(f"> [!note]- {label}\n" + "\n".join(lined))
    return "\n\n".join(out)


def render(m, memos, uid2name, out_ref, comments_on, id2uid):
    fm = ["---", f'uid: "{m["uid"]}"']
    if m["title"]:
        fm.append("title: " + json.dumps(m["title"], ensure_ascii=False))
    fm += [
        f"created: {m['created']}",
        f"updated: {m['updated']}",
        f"status: {m['status']}",
        f"visibility: {m['visibility']}",
    ]
    if m["pinned"]:
        fm.append("pinned: true")
    if m["tags"]:
        fm.append("tags: [" + ", ".join(json.dumps(t, ensure_ascii=False) for t in m["tags"]) + "]")
    aliases = [m["uid"]]
    if m["title"]:
        aliases.append(m["title"])
    for a in m["inner_aliases"]:
        if a and a not in aliases:
            aliases.append(a)
    fm.append("aliases: [" + ", ".join(json.dumps(a, ensure_ascii=False) for a in aliases) + "]")
    fm.append("---")

    parts = ["\n".join(fm), "", rewrite_all(m["content"], uid2name).rstrip()]

    co = callouts(comments_on.get(m["id"], []), memos, uid2name)
    if co:
        parts.append("\n## Comments\n\n" + co)

    refs = [id2uid.get(r) for r in out_ref.get(m["id"], [])]
    links = [uid2name[u] for u in refs if u in uid2name]
    if links:
        parts.append("\n## Related\n" + "\n".join(f"- [[{x}]]" for x in links))

    return "\n".join(parts).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    memos, id2uid, out_ref, comments_on = load(a.db)
    plan = plan_names(memos)
    uid2name = {m["uid"]: plan[m["id"]][1] for m in memos.values()}

    n_normal = sum(1 for m in memos.values() if plan[m["id"]][0] == "")
    n_arch = len(memos) - n_normal
    n_comments = sum(len(v) for v in comments_on.values())
    n_refs = sum(len(v) for v in out_ref.values())

    print(f"memos: {len(memos)}  ->  root {n_normal} / Archived {n_arch}")
    print(f"relations: REFERENCES {n_refs}  COMMENTS {n_comments}")
    print(f"target: {a.out}{'  [APPLY]' if a.apply else '  (dry run)'}")

    uid_named = [m for m in memos.values() if re.fullmatch(r"[a-z0-9-]*", plan[m["id"]][1]) and
                 plan[m["id"]][1] == slug(m["uid"])]
    if uid_named:
        print(f"note: {len(uid_named)} note(s) have no usable title -> uid-named")

    seen = {}
    for m in memos.values():
        k = plan[m["id"]]
        if k in seen:
            print(f"!! NAME COLLISION {k}: uids {seen[k]} vs {m['uid']}")
        seen[k] = m["uid"]

    if not a.apply:
        for m in list(memos.values())[:8]:
            d, nm = plan[m["id"]]
            print(f"  {'Archived/' if d else ''}{nm}.md  <- {(m['title'] or m['uid'])[:50]}")
        print("\n(dry run - re-run with --apply)")
        return

    for m in memos.values():
        d, nm = plan[m["id"]]
        target_dir = os.path.join(a.out, d) if d else a.out
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, nm + ".md"), "w", encoding="utf-8") as fh:
            fh.write(render(m, memos, uid2name, out_ref, comments_on, id2uid))

    idx = ["# Memos Export Index\n"]
    for label, want in (("NORMAL", ""), ("ARCHIVED", "Archived")):
        grp = sorted(
            (m for m in memos.values() if plan[m["id"]][0] == want),
            key=lambda x: x["created"],
        )
        idx.append(f"\n## {label} ({len(grp)})\n")
        for m in grp:
            d, nm = plan[m["id"]]
            t = f" - {m['title']}" if m["title"] else ""
            idx.append(f"- {m['created'][:10]} - [[{nm}]]{t}")
    with open(os.path.join(a.out, "_Index.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(idx) + "\n")

    print(f"wrote {len(memos)} notes + _Index.md to {a.out}")


if __name__ == "__main__":
    main()
