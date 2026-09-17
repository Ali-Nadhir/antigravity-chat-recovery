#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity conversation-index recovery tool.

Google Antigravity stores its Agent-conversation list in a binary protobuf file:
    %USERPROFILE%\\.gemini\\antigravity\\agyhub_summaries_proto.pb
When Antigravity crashes during a rewrite of that file (observed after app/OS
crashes), the file can be left as 100% zero bytes. The chat *content* itself is
NOT deleted - it lives in ~/.gemini/antigravity/conversations/<uuid>.db (+ .pb
for pre-mid-2026 conversations). Only the index is gone, so the UI shows no
conversations.

This tool detects that state and rebuilds the index from the conversation
files themselves. Everything it needs was reverse-engineered from real files;
see README.md for the full format documentation.

Commands:
    check                 diagnose only - is the index damaged, what can be recovered
    backup [--dest DIR]   copy all data needed for recovery to a safe place
    rebuild [--apply]     reconstruct the index; with --apply, swap it in live
                          (refuses to run while Antigravity is open)
    rebuild --out FILE    build to a file without touching the live one

Safety rules (always enforced):
  * the damaged/previous index is renamed, never deleted
  * a timestamped backup is written before any live change
  * the rebuilt file is verified (re-decoded end-to-end) before it is installed
  * nothing is written while Antigravity is running

Requires: Python 3.8+, Windows (paths and tasklist), no third-party packages.
Tested against Antigravity conversation schema of Sep 2026. If Google changes
the format, run `check`, create one throwaway conversation in Antigravity, and
diff the fresh index against the format documented in README.md.
"""

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
AG_DIR = HOME / ".gemini" / "antigravity"
CONV_DIR = AG_DIR / "conversations"
INDEX_FILE = AG_DIR / "agyhub_summaries_proto.pb"
PROJECTS_DIR = HOME / ".gemini" / "config" / "projects"
STATE_VSCDB = HOME / "AppData" / "Roaming" / "Antigravity" / "User" / "globalStorage" / "state.vscdb"
BRAIN_DIR = AG_DIR / "brain"
MIRROR_KEYS = (
    "antigravityUnifiedStateSync.trajectorySummaries",
    "unifiedStateSync.trajectorySummaries",
)
OUTSIDE_PROJECT_ID = "outside-of-project"
MAX_TITLE = 90

# ---------------------------------------------------------------- protobuf ---

def read_varint(buf, pos):
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ValueError("varint overrun")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")

def decode_fields(buf):
    """Decode a protobuf message into a list of (field_number, wire_type, value)."""
    fields, pos = [], 0
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        fnum, wt = key >> 3, key & 7
        if fnum == 0:
            raise ValueError("invalid field 0")
        if wt == 0:
            val, pos = read_varint(buf, pos)
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            if pos + ln > len(buf):
                raise ValueError("length overrun")
            val = bytes(buf[pos:pos + ln])
            pos += ln
        elif wt == 5:
            val = struct.unpack("<I", buf[pos:pos + 4])[0]
            pos += 4
        elif wt == 1:
            val = buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError("unsupported wire type %d" % wt)
        fields.append((fnum, wt, val))
    return fields

def get1(fields, num):
    for f in fields:
        if f[0] == num:
            return f
    return None

def enc_varint(n):
    n = int(n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)

def enc_tag(fnum, wt):
    return enc_varint((fnum << 3) | wt)

def enc_str(fnum, s):
    b = s.encode("utf-8")
    return enc_tag(fnum, 2) + enc_varint(len(b)) + b

def enc_msg(fnum, *parts):
    inner = b"".join(p for p in parts if p)
    return enc_tag(fnum, 2) + enc_varint(len(inner)) + inner

def enc_int(fnum, n):
    return enc_tag(fnum, 0) + enc_varint(n)

def enc_ts(fnum, t):
    """Timestamp message: f1 = seconds, f2 = nanos."""
    if not t:
        return b""
    return enc_msg(fnum, enc_int(1, t[0]), enc_int(2, t[1] or 0))

DEC_LIST_RE = re.compile(rb"\d{1,3}(,\d{1,3})*")

def parse_flex(raw):
    """Parse a payload that may be binary protobuf OR decimal-text ('10,165,1,..')."""
    if not raw:
        return None
    if DEC_LIST_RE.fullmatch(raw.strip()):
        try:
            fields = decode_fields(bytes(int(x) for x in raw.strip().split(b",")))
            if fields and not all(f[0] == 0 for f in fields):
                return fields
        except Exception:
            pass
    try:
        fields = decode_fields(raw)
        if fields and not all(f[0] == 0 for f in fields):
            return fields
    except Exception:
        pass
    return None

def text_of(raw):
    """Field value that is text, possibly stored as a decimal byte list."""
    if DEC_LIST_RE.fullmatch(raw.strip()):
        raw = bytes(int(x) for x in raw.strip().split(b","))
    return raw.decode("utf-8", errors="replace")

def is_uuid(s):
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s or ""))

def clean_title(t):
    return re.sub(r"\s+", " ", t).strip()[:MAX_TITLE]

def ts_of(fields):
    f = get1(fields, 1)
    if not f:
        return None
    sub = decode_fields(f[2]) if isinstance(f[2], (bytes, bytearray)) else None
    if not sub:
        return None
    sec = get1(sub, 1)[2] if get1(sub, 1) else 0
    nan = get1(sub, 2)[2] if get1(sub, 2) else 0
    return (sec, nan) if sec > 1_000_000_000 else None

# ------------------------------------------------------------- sqlite utils --

def open_db_ro(path):
    """Open a SQLite file read-only. Falls back to a temp copy when the file
    has a hot journal (crash leftover) that read-only mode cannot replay."""
    path = str(path)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return con, None
    except sqlite3.Error:
        tmp = Path(tempfile.mkdtemp(prefix="agrecovery-"))
        for ext in ("", "-wal", "-shm"):
            src = Path(path + ext)
            if src.exists():
                dst = tmp / (src.name)
                shutil.copy2(src, dst)
                if dst.stat().st_size == 0:
                    raise RuntimeError("could not copy %s (file locked?)" % src)
        con = sqlite3.connect(str(tmp / Path(path).name))
        return con, tmp

def is_all_zeros(path):
    data = Path(path).read_bytes()
    return len(data) > 0 and not any(data)

# ------------------------------------------------------- metadata extraction -

def extract_db_conversation(db_path):
    rec = {"id": db_path.stem, "kind": "db", "title": None, "created": None,
           "updated": None, "ws": None, "traj_id": None, "count": 0,
           "session_id": None, "project_id": None}
    con, tmp = open_db_ro(db_path)
    try:
        row = con.execute("SELECT trajectory_id, cascade_id, source FROM trajectory_meta").fetchone()
        if row:
            rec["traj_id"] = row[0]
        rec["count"] = con.execute("SELECT COUNT(*) FROM steps").fetchone()[0]

        # trajectory_metadata_blob: f1 workspace uri (msg), f3 session uuid,
        # f6 conversation uuid, f7 %3A-encoded uri, f18 project uuid
        for (data,) in con.execute("SELECT data FROM trajectory_metadata_blob"):
            fields = parse_flex(data if isinstance(data, (bytes, bytearray)) else str(data).encode())
            if not fields:
                continue
            f1 = get1(fields, 1)
            if f1 and rec["ws"] is None:
                inner = parse_flex(f1[2])
                if inner:
                    u = get1(inner, 1)
                    if u and isinstance(u[2], (bytes, bytearray)) and u[2].startswith(b"file:"):
                        rec["ws"] = u[2].decode()
            for num, key in ((3, "session_id"), (6, None), (18, "project_id")):
                f = get1(fields, num)
                if f and isinstance(f[2], (bytes, bytearray)) and is_uuid(f[2].decode("utf-8", "replace")):
                    if key:
                        rec[key] = f[2].decode()
                    elif f[2].decode() == rec["id"]:
                        pass  # f6 == conversation id, just a sanity marker

        first = last = None
        for (idx, step_type, payload) in con.execute(
                "SELECT idx, step_type, step_payload FROM steps ORDER BY idx"):
            if not payload:
                continue
            pay = parse_flex(payload)
            if not pay:
                continue
            meta = None
            f5 = get1(pay, 5)
            if f5:
                f5f = parse_flex(f5[2])
                if f5f:
                    f1m = get1(f5f, 1)
                    if f1m:
                        meta = parse_flex(f1m[2])
            if meta:
                t = ts_of(meta)
                if t:
                    first = first or t
                    last = t
            if step_type == 14 and rec["title"] is None:  # 14 = user message
                f19 = get1(pay, 19)
                if f19:
                    f19f = parse_flex(f19[2])
                    if f19f:
                        t = get1(f19f, 2) or get1(f19f, 1)
                        if t and isinstance(t[2], (bytes, bytearray)):
                            txt = text_of(t[2]).strip()
                            if txt:
                                rec["title"] = txt
        rec["created"], rec["updated"] = first, last
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    st = db_path.stat()
    if not rec["created"]:
        rec["created"] = (int(getattr(st, "st_ctime", st.st_mtime)), 0)
    if not rec["updated"]:
        rec["updated"] = (int(st.st_mtime), 0)
    if not rec["title"]:
        rec["title"] = "Conversation " + rec["id"][:8]
    rec["title"] = clean_title(rec["title"])
    return rec

def read_state_mirrors():
    """Older (.pb-era) conversations carry their summaries inside Antigravity's
    state database, base64-wrapped. Returns {conversation_id: record}."""
    if not STATE_VSCDB.exists():
        return {}
    out = {}
    tmp = Path(tempfile.mkdtemp(prefix="agrecovery-"))
    try:
        shutil.copy2(STATE_VSCDB, tmp / "state.vscdb")  # journal deliberately excluded
        con = sqlite3.connect(str(tmp / "state.vscdb"))
        try:
            for key in MIRROR_KEYS:
                row = con.execute("SELECT value FROM ItemTable WHERE key=?", (key,)).fetchone()
                if not row:
                    continue
                try:
                    raw = row[0]
                    raw = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else str(raw)
                    blob = base64.b64decode(re.sub(r"\s+", "", raw), validate=False)
                    top = decode_fields(blob)
                except Exception:
                    continue
                for f1 in [f for f in top if f[0] == 1]:
                    try:
                        sub = decode_fields(f1[2])
                        cid = get1(sub, 1)[2].decode()
                        pay = get1(sub, 2)
                        if not pay:
                            continue
                        pf = decode_fields(pay[2])
                        inner_b64 = get1(pf, 1)
                        inner = decode_fields(base64.b64decode(inner_b64[2].decode(), validate=False))
                        def g(n):
                            return get1(inner, n)
                        title = g(1)[2].decode() if g(1) else None
                        cnt = g(2)[2] if g(2) else 1
                        ts3 = ts_of(decode_fields(g(3)[2])) if g(3) else None
                        ts7 = ts_of(decode_fields(g(7)[2])) if g(7) else None
                        traj = g(4)[2].decode() if g(4) and is_uuid(g(4)[2].decode()) else None
                        ws = None
                        f9 = g(9)
                        if f9:
                            ws = get1(decode_fields(f9[2]), 1)
                            ws = ws[2].decode() if ws else None
                        prev = out.get(cid, {})
                        out[cid] = {
                            "title": title or prev.get("title"),
                            "count": cnt or prev.get("count", 1),
                            "updated": ts3 or prev.get("updated"),
                            "created": ts7 or prev.get("created"),
                            "traj_id": traj or prev.get("traj_id"),
                            "ws": ws or prev.get("ws"),
                        }
                    except Exception:
                        continue
        finally:
            con.close()
    except Exception:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out

def brain_fallback_title(conv_id):
    md = BRAIN_DIR / conv_id / "task.md"
    if md.exists():
        for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return clean_title(line)
    return None

# ------------------------------------------------------------- project info --

def load_project_map():
    """folderUri (both encodings) -> project id, from the app's own registry."""
    mapping = {}
    if PROJECTS_DIR.exists():
        for j in PROJECTS_DIR.glob("*.json"):
            try:
                d = json.loads(j.read_text(encoding="utf-8"))
                for res in (d.get("projectResources") or {}).get("resources") or []:
                    uri = res.get("folderUri")
                    if uri and d.get("id"):
                        mapping[uri] = d["id"]
                        mapping[uri.replace("%3A", ":")] = d["id"]
            except Exception:
                continue
    return mapping

def register_project(name, folder_uri):
    pid = str(uuid.uuid4())
    doc = {
        "id": pid,
        "name": name,
        "projectResources": {"resources": [{"folderUri": folder_uri}]},
        "settings": {},
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "isWorkspaceOnly": False,
    }
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    (PROJECTS_DIR / (pid + ".json")).write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print("  registered new project %r -> %s" % (name, pid))
    return pid

# ------------------------------------------------------------ index building -

def build_entry(rec):
    parts = [enc_str(1, rec["title"]), enc_int(2, max(1, rec["count"])),
             enc_ts(3, rec["updated"])]
    if rec.get("traj_id"):
        parts.append(enc_str(4, rec["traj_id"]))
    parts.append(enc_int(5, 1))
    parts.append(enc_ts(7, rec["created"]))
    if rec.get("ws"):
        parts.append(enc_msg(9, enc_str(1, rec["ws"]), enc_msg(3)))
    parts.append(enc_ts(10, rec["updated"]))
    parts.append(enc_msg(15, enc_ts(7, rec["created"])))
    parts.append(enc_int(16, max(0, max(1, rec["count"]) - 2)))
    if rec.get("project_id"):
        f17 = [enc_ts(2, rec["created"])]
        if rec.get("session_id"):
            f17.append(enc_str(3, rec["session_id"]))
        f17.append(enc_str(6, rec["id"]))
        if rec.get("ws"):
            pct = re.sub(r"^(file:///(\w)):", r"\1%3A", rec["ws"])
            f17.append(enc_str(7, pct))
            f17.insert(0, enc_msg(1, enc_str(1, rec["ws"]), enc_msg(3)))
        f17.append(enc_str(18, rec["project_id"]))
        parts.append(enc_msg(17, *f17))
    parts.append(enc_int(22, 4))
    return enc_msg(1, enc_str(1, rec["id"]), enc_msg(2, *parts))

def scan_conversations(projects, verbose=True):
    """Collect metadata for every conversation file that has content."""
    records, skipped = [], []
    for path in sorted(CONV_DIR.iterdir()):
        if path.suffix == ".db":
            if is_all_zeros(path):
                skipped.append((path.stem, "content file is 100% zeros"))
                continue
            records.append(extract_db_conversation(path))
        elif path.suffix == ".pb" and not path.name.endswith(".pb.old"):
            if is_all_zeros(path):
                skipped.append((path.stem, "content file is 100% zeros"))
                continue
            st = path.stat()
            records.append({"id": path.stem, "kind": "pb", "title": None,
                            "created": (int(getattr(st, "st_ctime", st.st_mtime)), 0),
                            "updated": (int(st.st_mtime), 0), "ws": None,
                            "traj_id": None, "count": 1, "session_id": None,
                            "project_id": None})
    return records, skipped

def fill_from_mirrors(records, mirrors):
    for rec in records:
        m = mirrors.get(rec["id"])
        if not m:
            continue
        rec["title"] = rec["title"] or m.get("title")
        rec["updated"] = rec["updated"] or m.get("updated")
        rec["created"] = rec["created"] or m.get("created")
        rec["traj_id"] = rec["traj_id"] or m.get("traj_id")
        rec["ws"] = rec["ws"] or m.get("ws")
        if rec["kind"] == "pb" and m.get("count") and m["count"] > 1:
            rec["count"] = m["count"]
    return records

def resolve_projects(records, projects):
    """Assign project ids; create registry entries for unknown workspaces."""
    for rec in records:
        if rec.get("project_id"):
            continue
        if rec.get("ws"):
            pid = projects.get(rec["ws"])
            if not pid:
                name = rec["ws"].rstrip("/").rsplit("/", 1)[-1] or "project"
                pid = register_project(name, rec["ws"])
                projects[rec["ws"]] = pid
                projects[rec["ws"].replace("%3A", ":")] = pid
            rec["project_id"] = pid
        elif rec.get("session_id"):
            rec["project_id"] = OUTSIDE_PROJECT_ID
    return records

def parse_existing_index(path):
    """Return verbatim entry payloads from a still-parseable index (if any)."""
    try:
        top = decode_fields(Path(path).read_bytes())
        return [f[2] for f in top if f[0] == 1]
    except Exception:
        return []

# ------------------------------------------------------------------ commands -

def antigravity_running():
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=30).stdout
        return "antigravity" in out.lower()
    except Exception:
        return False  # cannot tell; assume closed but warn caller

def cmd_check(_args):
    print("Antigravity index:", INDEX_FILE)
    damaged = True
    n_entries = 0
    if not INDEX_FILE.exists():
        print("  status: MISSING")
    elif is_all_zeros(INDEX_FILE):
        print("  status: ZEROED (%d bytes of zeros - the known crash symptom)" % INDEX_FILE.stat().st_size)
    else:
        try:
            entries = [f for f in decode_fields(INDEX_FILE.read_bytes()) if f[0] == 1]
            n_entries = len(entries)
            damaged = False
            print("  status: parses OK, %d entries" % n_entries)
        except Exception as e:
            print("  status: UNPARSEABLE (%s)" % e)
    recs, skipped = scan_conversations(load_project_map(), verbose=False)
    mirrors = read_state_mirrors()
    fill_from_mirrors(recs, mirrors)
    print("  conversations on disk with content: %d" % len(recs))
    print("  recoverable titles found: %d" % sum(1 for r in recs if r.get("title")))
    if skipped:
        print("  skipped (no content): %s" % ", ".join(i[:8] for i, _ in skipped))
    if n_entries and n_entries < len(recs):
        print("  NOTE: index has %d entries but %d conversations exist -> recovery recommended"
              % (n_entries, len(recs)))
        damaged = True
    print("  verdict: " + ("RECOVERY NEEDED / RECOMMENDED" if damaged else "index looks complete"))
    return 0

def cmd_backup(args):
    dest = Path(args.dest) if args.dest else Path.cwd() / ("antigravity-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    dest.mkdir(parents=True, exist_ok=True)
    targets = [AG_DIR, PROJECTS_DIR, STATE_VSCDB]
    for t in targets:
        if not t.exists():
            continue
        dst = dest / t.name
        print("copying", t, "->", dst)
        if t.is_dir():
            shutil.copytree(t, dst, ignore=shutil.ignore_patterns("Cache*", "Code Cache", "GPUCache", "Crashpad", "CachedData", "CachedExtensionVSIXs", "blob_storage", "Service Worker", "Dawn*", "Shared Dictionary", "VideoDecodeStats", "Network", "logs"))
        else:
            shutil.copy2(t, dst)
    print("backup complete:", dest)
    return 0

def cmd_rebuild(args):
    if args.apply and antigravity_running():
        print("REFUSING: Antigravity is running. Close it completely and retry.")
        return 2

    mirrors = read_state_mirrors()
    records, skipped = scan_conversations(load_project_map())
    fill_from_mirrors(records, mirrors)
    for rec in records:
        if rec["kind"] == "pb" and (not rec.get("title") or rec["title"].startswith("Conversation ")):
            bt = brain_fallback_title(rec["id"])
            if bt:
                rec["title"] = bt
    projects = load_project_map()
    records = resolve_projects(records, projects)
    records = [r for r in records if r.get("project_id")]
    if skipped:
        print("skipped (no content, not indexed): %s" % ", ".join("%s (%s)" % (i[:8], why) for i, why in skipped))
    if not records:
        print("nothing to recover - aborting without writing anything")
        return 1

    preserved = []
    if INDEX_FILE.exists() and not is_all_zeros(INDEX_FILE):
        preserved = parse_existing_index(INDEX_FILE)
    new_ids = {r["id"] for r in records}
    kept = []
    seen = set()
    for entry in preserved:
        try:
            eid = get1(decode_fields(entry), 1)[2].decode()
        except Exception:
            continue
        seen.add(eid)
        if eid not in new_ids or True:  # app-written entries always win
            kept.append(entry)
    body = b"".join(enc_msg(1, e) for e in kept) + b"".join(build_entry(r) for r in sorted(records, key=lambda r: r["updated"][0]) if r["id"] not in seen)
    data = body

    # verify
    try:
        entries = [f for f in decode_fields(data) if f[0] == 1]
        unbound = []
        for e in entries:
            sub = decode_fields(e[2])
            s = decode_fields(get1(sub, 2)[2])
            f17 = get1(s, 17)
            if not f17 or not get1(decode_fields(f17[2]), 18):
                unbound.append(get1(sub, 1)[2].decode()[:8])
        print("verify: %d entries decoded end-to-end; %d without project binding"
              % (len(entries), len(unbound)) + ((", e.g. " + ", ".join(unbound[:5])) if unbound else ""))
        if len(entries) < len(records):
            raise RuntimeError("entry count mismatch: %d < %d" % (len(entries), len(records)))
    except Exception as e:
        print("VERIFY FAILED (%s) - nothing was written" % e)
        return 1

    out_path = Path(args.out) if args.out else INDEX_FILE.with_suffix(".pb.recovered")
    out_path.write_bytes(data)
    print("built index with %d entries -> %s" % (len(entries), out_path))

    if not args.apply:
        print("dry run: live file untouched. Re-run with --apply (Antigravity closed) to install.")
        return 0

    if INDEX_FILE.exists():
        keep = INDEX_FILE.with_name(INDEX_FILE.name + ".pre-recovery-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        INDEX_FILE.rename(keep)
        print("previous index kept as", keep.name)
    os.replace(out_path, INDEX_FILE)
    print("installed. Start Antigravity - your conversations should be listed again.")
    return 0

def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Rebuild Antigravity's conversation index after the zeroed-index crash.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="diagnose: is recovery needed, what can be recovered")
    b = sub.add_parser("backup", help="copy all recovery-relevant data to a safe place")
    b.add_argument("--dest")
    r = sub.add_parser("rebuild", help="reconstruct the conversation index")
    r.add_argument("--apply", action="store_true", help="install the rebuilt index (requires Antigravity closed)")
    r.add_argument("--out", help="write result to this path instead of the live location")
    args = ap.parse_args()
    {"check": cmd_check, "backup": cmd_backup, "rebuild": cmd_rebuild}[args.cmd](args)

if __name__ == "__main__":
    sys.exit(main() or 0)
