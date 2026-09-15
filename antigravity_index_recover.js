#!/usr/bin/env node
/*
 * Antigravity conversation-index recovery tool (Node.js port).
 *
 * Google Antigravity stores its Agent-conversation list in a binary protobuf:
 *   %USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb
 * A crash during rewrite can leave that file as 100% zero bytes; the UI then
 * shows no conversations even though all chat content still exists in
 * ~/.gemini/antigravity/conversations/<uuid>.db (+ legacy .pb).
 *
 * Commands:
 *   check                 diagnose only
 *   backup [--dest DIR]   copy everything needed for recovery to a safe place
 *   rebuild [--apply]     reconstruct the index (with --apply, install it live)
 *   rebuild --out FILE    build to a file without touching the live one
 *
 * Safety: damaged index is renamed never deleted; timestamped backup before
 * any live change; result is re-decoded and verified before install; nothing
 * is written while Antigravity is running.
 *
 * Usage:  node antigravity_index_recover.js <command>
 * (Node >= 22.5; if node:sqlite is flagged on your build, prefix with
 *  --experimental-sqlite)
 */

"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execSync } = require("child_process");

let DatabaseSync;
try {
  ({ DatabaseSync } = require("node:sqlite"));
} catch (e) {
  console.error("This tool needs the built-in SQLite module (node:sqlite).");
  console.error("On Node 22.x run:  node --experimental-sqlite " + path.basename(process.argv[1]));
  process.exit(1);
}

const HOME = os.homedir();
const AG_DIR = path.join(HOME, ".gemini", "antigravity");
const CONV_DIR = path.join(AG_DIR, "conversations");
const INDEX_FILE = path.join(AG_DIR, "agyhub_summaries_proto.pb");
const PROJECTS_DIR = path.join(HOME, ".gemini", "config", "projects");
const STATE_VSCDB = path.join(HOME, "AppData", "Roaming", "Antigravity", "User", "globalStorage", "state.vscdb");
const BRAIN_DIR = path.join(AG_DIR, "brain");
const MIRROR_KEYS = [
  "antigravityUnifiedStateSync.trajectorySummaries",
  "unifiedStateSync.trajectorySummaries",
];
const OUTSIDE_PROJECT_ID = "outside-of-project";
const MAX_TITLE = 90;

// ------------------------------------------------------------ protobuf I/O --

function readVarint(b, pos) {
  let result = 0n, shift = 0n;
  for (;;) {
    if (pos >= b.length) throw new Error("varint overrun");
    const x = b[pos++];
    result |= BigInt(x & 0x7f) << shift;
    if ((x & 0x80) === 0) return [Number(result), pos];
    shift += 7n;
    if (shift > 70n) throw new Error("varint too long");
  }
}
function decodeFields(buf) {
  const fields = [];
  let pos = 0;
  while (pos < buf.length) {
    let tag;
    [tag, pos] = readVarint(buf, pos);
    const f = Math.floor(tag / 8), wt = tag % 8;
    if (f === 0) throw new Error("invalid field 0");
    if (wt === 0) { let v; [v, pos] = readVarint(buf, pos); fields.push({ f, wt, v }); }
    else if (wt === 2) { let l; [l, pos] = readVarint(buf, pos); l = Number(l); if (pos + l > buf.length) throw new Error("overrun"); fields.push({ f, wt, v: buf.slice(pos, pos + l) }); pos += l; }
    else if (wt === 5) { fields.push({ f, wt, v: buf.readUInt32LE(pos) }); pos += 4; }
    else if (wt === 1) { fields.push({ f, wt, v: buf.slice(pos, pos + 8) }); pos += 8; }
    else throw new Error("wire type " + wt);
  }
  return fields;
}
const get1 = (fields, n) => fields.find((x) => x.f === n);
const encVarint = (n) => { const out = []; n = BigInt(n); do { let b = Number(n & 0x7fn); n >>= 7n; if (n > 0n) b |= 0x80; out.push(b); } while (n > 0n); return Buffer.from(out); };
const encTag = (f, wt) => encVarint((BigInt(f) << 3n) | BigInt(wt));
const encStr = (f, s) => { const b = Buffer.from(String(s), "utf8"); return Buffer.concat([encTag(f, 2), encVarint(b.length), b]); };
const encMsg = (f, ...parts) => { const inner = Buffer.concat(parts.filter(Boolean)); return Buffer.concat([encTag(f, 2), encVarint(inner.length), inner]); };
const encInt = (f, v) => Buffer.concat([encTag(f, 0), encVarint(v)]);
const encTs = (f, t) => (t ? encMsg(f, encInt(1, t.sec), encInt(2, t.nan || 0)) : null);

const DEC_LIST_RE = /^\d{1,3}(,\d{1,3})*$/;
const parseDecList = (s) => Buffer.from(s.trim().split(",").map(Number));

function parseFlex(raw) {
  if (!raw || !raw.length) return null;
  const asText = raw.toString("utf8");
  if (DEC_LIST_RE.test(asText.trim())) {
    try {
      const fl = decodeFields(parseDecList(asText));
      if (fl.length && !fl.every((x) => x.f === 0)) return fl;
    } catch (e) { /* fall through */ }
  }
  try {
    const fl = decodeFields(raw);
    if (fl.length && !fl.every((x) => x.f === 0)) return fl;
  } catch (e) { /* fall through */ }
  return null;
}
function textOf(raw) {
  const s = raw.toString("utf8");
  return DEC_LIST_RE.test(s.trim()) ? parseDecList(s).toString("utf8") : s;
}
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const isUuid = (s) => UUID_RE.test(s || "");
const cleanTitle = (t) => String(t).replace(/\r?\n+/g, " ").replace(/\s+/g, " ").trim().slice(0, MAX_TITLE);
function tsOf(fields) {
  const f = get1(fields, 1);
  if (!f) return null;
  const sub = parseFlex(f.v);
  if (!sub) return null;
  const sec = get1(sub, 1) ? get1(sub, 1).v : 0;
  const nan = get1(sub, 2) ? get1(sub, 2).v : 0;
  return sec > 1e9 ? { sec, nan } : null;
}
const isAllZeros = (p) => { const b = fs.readFileSync(p); return b.length > 0 && !b.some((x) => x); };

// ------------------------------------------------------------- sqlite utils -

function openDbRo(dbPath) {
  try {
    const db = new DatabaseSync(dbPath, { readOnly: true });
    db.prepare("SELECT 1 FROM sqlite_master LIMIT 1").get();
    return { db, tmp: null };
  } catch (e) {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agrecovery-"));
    for (const ext of ["", "-wal", "-shm"]) {
      const src = dbPath + ext;
      if (fs.existsSync(src)) {
        const dst = path.join(tmp, path.basename(src));
        fs.copyFileSync(src, dst);
        if (fs.statSync(dst).size === 0) throw new Error("could not copy " + src + " (locked?)");
      }
    }
    return { db: new DatabaseSync(path.join(tmp, path.basename(dbPath))), tmp };
  }
}

// ------------------------------------------------------ metadata extraction -

function extractDbConversation(dbPath) {
  const rec = { id: path.basename(dbPath, ".db"), kind: "db", title: null, created: null,
    updated: null, ws: null, trajId: null, count: 0, sessionId: null, projectId: null };
  const { db, tmp } = openDbRo(dbPath);
  try {
    const tm = db.prepare("SELECT trajectory_id, cascade_id, source FROM trajectory_meta").get();
    if (tm) rec.trajId = tm.trajectory_id;
    rec.count = db.prepare("SELECT COUNT(*) c FROM steps").get().c;

    for (const row of db.prepare("SELECT data FROM trajectory_metadata_blob").all()) {
      const fields = parseFlex(Buffer.from(row.data));
      if (!fields) continue;
      const f1 = get1(fields, 1);
      if (f1 && !rec.ws) {
        const inner = parseFlex(f1.v);
        if (inner) {
          const u = get1(inner, 1);
          if (u && u.v.slice(0, 5).toString() === "file:") rec.ws = u.v.toString("utf8");
        }
      }
      for (const [num, key] of [[3, "sessionId"], [18, "projectId"]]) {
        const f = get1(fields, num);
        if (f && isUuid(f.v.toString("utf8")) && !rec[key]) rec[key] = f.v.toString("utf8");
      }
    }

    let first = null, last = null;
    for (const row of db.prepare("SELECT step_type, step_payload FROM steps ORDER BY idx").all()) {
      if (!row.step_payload) continue;
      const pay = parseFlex(Buffer.from(row.step_payload));
      if (!pay) continue;
      const f5 = get1(pay, 5);
      let meta = null;
      if (f5) {
        const f5f = parseFlex(f5.v);
        const f1m = f5f && get1(f5f, 1);
        if (f1m) meta = parseFlex(f1m.v);
      }
      if (meta) {
        const t = tsOf(meta);
        if (t) { first = first || t; last = t; }
      }
      if (row.step_type === 14 && rec.title === null) { // 14 = user message
        const f19 = get1(pay, 19);
        if (f19) {
          const f19f = parseFlex(f19.v);
          const t = f19f && (get1(f19f, 2) || get1(f19f, 1));
          if (t && t.v) {
            const txt = textOf(t.v).trim();
            if (txt) rec.title = txt;
          }
        }
      }
    }
    rec.created = first; rec.updated = last;
  } finally {
    db.close();
    if (tmp) fs.rmSync(tmp, { recursive: true, force: true });
  }
  const st = fs.statSync(dbPath);
  if (!rec.created) rec.created = { sec: Math.floor(st.ctimeMs / 1000), nan: 0 };
  if (!rec.updated) rec.updated = { sec: Math.floor(st.mtimeMs / 1000), nan: 0 };
  if (!rec.title) rec.title = "Conversation " + rec.id.slice(0, 8);
  rec.title = cleanTitle(rec.title);
  return rec;
}

function readStateMirrors() {
  // Older (.pb-era) conversations keep their summaries inside Antigravity's
  // state database, base64-wrapped. Copy WITHOUT the journal (hot journal breaks read-only).
  if (!fs.existsSync(STATE_VSCDB)) return {};
  const out = {};
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agrecovery-"));
  try {
    fs.copyFileSync(STATE_VSCDB, path.join(tmp, "state.vscdb"));
    const db = new DatabaseSync(path.join(tmp, "state.vscdb"), { readOnly: true });
    try {
      for (const key of MIRROR_KEYS) {
        const row = db.prepare("SELECT value FROM ItemTable WHERE key=?").get(key);
        if (!row) continue;
        let top;
        try { top = decodeFields(Buffer.from(String(row.value).replace(/\s+/g, ""), "base64")); } catch (e) { continue; }
        for (const e of top.filter((x) => x.f === 1)) {
          try {
            const sub = decodeFields(e.v);
            const cid = get1(sub, 1).v.toString();
            const pay = get1(sub, 2);
            if (!pay) continue;
            const pf = decodeFields(pay.v);
            const innerB64 = get1(pf, 1);
            const inner = decodeFields(Buffer.from(innerB64.v.toString("utf8"), "base64"));
            const g = (n) => get1(inner, n);
            const prev = out[cid] || {};
            const f9 = g(9);
            let ws = null;
            if (f9) { const u = get1(decodeFields(f9.v), 1); ws = u ? u.v.toString() : null; }
            out[cid] = {
              title: (g(1) ? g(1).v.toString() : null) || prev.title,
              count: (g(2) ? g(2).v : 1) || prev.count || 1,
              updated: (g(3) ? tsOf(decodeFields(g(3).v)) : null) || prev.updated,
              created: (g(7) ? tsOf(decodeFields(g(7).v)) : null) || prev.created,
              trajId: (g(4) && isUuid(g(4).v.toString()) ? g(4).v.toString() : null) || prev.trajId,
              ws: ws || prev.ws,
            };
          } catch (err) { /* skip damaged entry */ }
        }
      }
    } finally { db.close(); }
  } catch (err) { /* state db unreadable - mirrors unavailable */ }
  finally { fs.rmSync(tmp, { recursive: true, force: true }); }
  return out;
}

function brainFallbackTitle(convId) {
  const md = path.join(BRAIN_DIR, convId, "task.md");
  if (!fs.existsSync(md)) return null;
  for (const line of fs.readFileSync(md, "utf8").split(/\r?\n/)) {
    const t = line.trim().replace(/^#+\s*/, "").trim();
    if (t) return cleanTitle(t);
  }
  return null;
}

// -------------------------------------------------------------- projects ----

function loadProjectMap() {
  const mapping = {};
  if (fs.existsSync(PROJECTS_DIR)) {
    for (const f of fs.readdirSync(PROJECTS_DIR).filter((f) => f.endsWith(".json"))) {
      try {
        const d = JSON.parse(fs.readFileSync(path.join(PROJECTS_DIR, f), "utf8"));
        for (const res of ((d.projectResources || {}).resources) || []) {
          if (res.folderUri && d.id) {
            mapping[res.folderUri] = d.id;
            mapping[res.folderUri.replace(/%3A/g, ":")] = d.id;
          }
        }
      } catch (e) { /* skip bad file */ }
    }
  }
  return mapping;
}
function registerProject(name, folderUri) {
  const id = [8, 4, 4, 4, 12].map((l) => Array.from({ length: l }, () => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("")).join("-");
  fs.mkdirSync(PROJECTS_DIR, { recursive: true });
  fs.writeFileSync(path.join(PROJECTS_DIR, id + ".json"), JSON.stringify({
    id, name,
    projectResources: { resources: [{ folderUri }] },
    settings: {},
    updatedAt: new Date().toISOString(),
    isWorkspaceOnly: false,
  }, null, 2));
  console.log("  registered new project %j -> %s", name, id);
  return id;
}

// ---------------------------------------------------------- index building --

function buildEntry(rec) {
  const parts = [];
  parts.push(encStr(1, rec.title));
  parts.push(encInt(2, Math.max(1, rec.count)));
  parts.push(encTs(3, rec.updated));
  if (rec.trajId) parts.push(encStr(4, rec.trajId));
  parts.push(encInt(5, 1));
  parts.push(encTs(7, rec.created));
  if (rec.ws) parts.push(encMsg(9, encStr(1, rec.ws), encMsg(3)));
  parts.push(encTs(10, rec.updated));
  parts.push(encMsg(15, encTs(7, rec.created)));
  parts.push(encInt(16, Math.max(0, Math.max(1, rec.count) - 2)));
  if (rec.projectId) {
    const f17 = [encTs(2, rec.created)];
    if (rec.sessionId) f17.push(encStr(3, rec.sessionId));
    f17.push(encStr(6, rec.id));
    if (rec.ws) f17.push(encStr(7, rec.ws.replace(/^(file:\/\/\/(\w)):/, "$1%3A")));
    f17.push(encStr(18, rec.projectId));
    if (rec.ws) f17.unshift(encMsg(1, encStr(1, rec.ws), encMsg(3)));
    parts.push(encMsg(17, ...f17));
  }
  parts.push(encInt(22, 4));
  return encMsg(1, encStr(1, rec.id), encMsg(2, ...parts));
}

function scanConversations() {
  const records = [], skipped = [];
  for (const name of fs.readdirSync(CONV_DIR).sort()) {
    const p = path.join(CONV_DIR, name);
    if (!/\.(db|pb)$/.test(name)) continue;
    const id = name.replace(/\.(db|pb)$/, "");
    if (isAllZeros(p)) { skipped.push([id, "content file is 100% zeros"]); continue; }
    if (name.endsWith(".db")) records.push(extractDbConversation(p));
    else records.push({ id, kind: "pb", title: null, created: null, updated: null, ws: null, trajId: null, count: 1, sessionId: null, projectId: null,
      _st: fs.statSync(p) });
  }
  return { records, skipped };
}

function resolveProjects(records, projects) {
  for (const rec of records) {
    if (rec.projectId) continue;
    if (rec.ws) {
      let pid = projects[rec.ws];
      if (!pid) {
        const name = rec.ws.replace(/\/+$/, "").split("/").pop() || "project";
        pid = registerProject(name, rec.ws);
        projects[rec.ws] = pid;
        projects[rec.ws.replace(/%3A/g, ":")] = pid;
      }
      rec.projectId = pid;
    } else if (rec.sessionId) {
      rec.projectId = OUTSIDE_PROJECT_ID;
    }
  }
  return records;
}

function parseExistingIndex(p) {
  try { return decodeFields(fs.readFileSync(p)).filter((x) => x.f === 1).map((x) => x.v); }
  catch (e) { return []; }
}

function antigravityRunning() {
  try {
    const out = execSync("tasklist", { encoding: "utf8", timeout: 30000 });
    return out.toLowerCase().includes("antigravity");
  } catch (e) { return false; }
}

// ---------------------------------------------------------------- commands --

function cmdCheck() {
  console.log("Antigravity index:", INDEX_FILE);
  let damaged = true, nEntries = 0;
  if (!fs.existsSync(INDEX_FILE)) console.log("  status: MISSING");
  else if (isAllZeros(INDEX_FILE)) console.log("  status: ZEROED (%d bytes of zeros - the known crash symptom)", fs.statSync(INDEX_FILE).size);
  else {
    try { nEntries = decodeFields(fs.readFileSync(INDEX_FILE)).filter((x) => x.f === 1).length; damaged = false; console.log("  status: parses OK, %d entries", nEntries); }
    catch (e) { console.log("  status: UNPARSEABLE (%s)", e.message); }
  }
  const { records, skipped } = scanConversations();
  const mirrors = readStateMirrors();
  for (const rec of records) {
    const m = mirrors[rec.id];
    if (m) { rec.title = rec.title || m.title; rec.updated = rec.updated || m.updated; rec.created = rec.created || m.created; rec.ws = rec.ws || m.ws; }
    if (rec.kind === "pb") { rec.title = rec.title || brainFallbackTitle(rec.id); }
    if (!rec.created) rec.created = { sec: Math.floor((rec._st ? rec._st.ctimeMs : Date.now()) / 1000), nan: 0 };
    if (!rec.updated) rec.updated = { sec: Math.floor((rec._st ? rec._st.mtimeMs : Date.now()) / 1000), nan: 0 };
  }
  console.log("  conversations on disk with content: %d", records.length);
  console.log("  recoverable titles found: %d", records.filter((r) => r.title).length);
  if (skipped.length) console.log("  skipped (no content): %s", skipped.map(([i]) => i.slice(0, 8)).join(", "));
  if (nEntries && nEntries < records.length) { console.log("  NOTE: index has %d entries but %d conversations exist -> recovery recommended", nEntries, records.length); damaged = true; }
  console.log("  verdict: " + (damaged ? "RECOVERY NEEDED / RECOMMENDED" : "index looks complete"));
}

function cmdBackup(args) {
  const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
  const dest = path.resolve(args.dest || path.join(process.cwd(), "antigravity-backup-" + stamp));
  fs.mkdirSync(dest, { recursive: true });
  const skip = /Cache|Crashpad|CachedData|CachedExtensionVSIXs|blob_storage|Service Worker|Dawn|Shared Dictionary|VideoDecodeStats|Network|^logs$|Code Cache/i;
  for (const t of [AG_DIR, PROJECTS_DIR, STATE_VSCDB]) {
    if (!fs.existsSync(t)) continue;
    const dst = path.join(dest, path.basename(t));
    console.log("copying", t, "->", dst);
    if (fs.statSync(t).isDirectory()) {
      fs.cpSync(t, dst, { recursive: true, filter: (src) => !skip.test(path.basename(src)) });
    } else fs.copyFileSync(t, dst);
  }
  console.log("backup complete:", dest);
}

function cmdRebuild(args) {
  if (args.apply && antigravityRunning()) {
    console.log("REFUSING: Antigravity is running. Close it completely and retry.");
    process.exitCode = 2;
    return;
  }
  const mirrors = readStateMirrors();
  const { records, skipped } = scanConversations();
  for (const rec of records) {
    const m = mirrors[rec.id];
    if (m) { rec.title = rec.title || m.title; rec.updated = rec.updated || m.updated; rec.created = rec.created || m.created; rec.trajId = rec.trajId || m.trajId; rec.ws = rec.ws || m.ws; if (rec.kind === "pb" && m.count > 1) rec.count = m.count; }
    if (rec.kind === "pb") rec.title = rec.title || brainFallbackTitle(rec.id);
    if (!rec.created) rec.created = { sec: Math.floor((rec._st ? rec._st.ctimeMs : Date.now()) / 1000), nan: 0 };
    if (!rec.updated) rec.updated = { sec: Math.floor((rec._st ? rec._st.mtimeMs : Date.now()) / 1000), nan: 0 };
    if (!rec.title) rec.title = "Conversation " + rec.id.slice(0, 8);
    rec.title = cleanTitle(rec.title);
  }
  const projects = loadProjectMap();
  resolveProjects(records, projects);
  const bound = records.filter((r) => r.projectId);
  if (skipped.length) console.log("skipped (no content, not indexed): %s", skipped.map(([i, why]) => i.slice(0, 8) + " (" + why + ")").join(", "));
  if (!bound.length) { console.log("nothing to recover - aborting without writing anything"); process.exitCode = 1; return; }

  // preserve verbatim whatever the running app still knows
  const kept = [];
  const seen = new Set();
  if (fs.existsSync(INDEX_FILE) && !isAllZeros(INDEX_FILE)) {
    for (const entry of parseExistingIndex(INDEX_FILE)) {
      try { const id = get1(decodeFields(entry), 1).v.toString(); seen.add(id); kept.push(entry); } catch (e) { /* skip */ }
    }
  }
  const data = Buffer.concat([...kept.map((v) => encMsg(1, v)),
    ...bound.filter((r) => !seen.has(r.id)).sort((a, b) => a.updated.sec - b.updated.sec).map(buildEntry)]);

  // verify end-to-end
  let entries;
  try {
    entries = decodeFields(data).filter((x) => x.f === 1);
    const unbound = [];
    for (const e of entries) {
      const sub = decodeFields(e.v);
      const sum = decodeFields(get1(sub, 2).v);
      const f17 = get1(sum, 17);
      if (!f17 || !get1(decodeFields(f17.v), 18)) unbound.push(get1(sub, 1).v.toString().slice(0, 8));
    }
    console.log("verify: %d entries decoded end-to-end; %d without project binding%s",
      entries.length, unbound.length, unbound.length ? " (e.g. " + unbound.slice(0, 5).join(",") + ")" : "");
    if (entries.length < bound.length) throw new Error("entry count mismatch");
  } catch (e) {
    console.log("VERIFY FAILED (%s) - nothing was written", e.message);
    process.exitCode = 1;
    return;
  }

  const outPath = args.out || INDEX_FILE + ".recovered";
  fs.writeFileSync(outPath, data);
  console.log("built index with %d entries -> %s", entries.length, outPath);

  if (!args.apply) { console.log("dry run: live file untouched. Re-run with --apply (Antigravity closed) to install."); return; }
  if (fs.existsSync(INDEX_FILE)) {
    const keep = INDEX_FILE + ".pre-recovery-" + new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    fs.renameSync(INDEX_FILE, keep);
    console.log("previous index kept as", path.basename(keep));
  }
  fs.renameSync(outPath, INDEX_FILE);
  console.log("installed. Start Antigravity - your conversations should be listed again.");
}

function main() {
  const [, , cmd, ...rest] = process.argv;
  const args = {};
  for (let i = 0; i < rest.length; i++) {
    if (rest[i] === "--apply") args.apply = true;
    else if (rest[i] === "--dest") args.dest = rest[++i];
    else if (rest[i] === "--out") args.out = rest[++i];
  }
  if (cmd === "check") return cmdCheck();
  if (cmd === "backup") return cmdBackup(args);
  if (cmd === "rebuild") return cmdRebuild(args);
  console.log("usage: node antigravity_index_recover.js <check|backup|rebuild> [--apply] [--out FILE] [--dest DIR]");
}

main();
