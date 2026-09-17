# Antigravity Chat Recovery

Rebuild Google Antigravity's conversation index after the "all my chats
disappeared" crash — from the conversation files that are still on your disk.

**Unofficial tool.** Not affiliated with or endorsed by Google. Use at your own
risk and **always keep a backup before running anything** (the `backup`
command does this for you).

## Symptoms

- You open Antigravity and the Agent Manager / conversation list is empty.
- Your chats "disappeared" after a crash or forced shutdown of the app.
- The file `%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb`
  exists with a plausible size, but is **100% zero bytes**.

## What actually happened

Antigravity keeps the chat *content* in per-conversation files:

```
%USERPROFILE%\.gemini\antigravity\conversations\<uuid>.db    (SQLite, mid-2026+)
%USERPROFILE%\.gemini\antigravity\conversations\<uuid>.pb    (legacy, encrypted)
```

The list you see in the UI comes from an *index* file next to them
(`agyhub_summaries_proto.pb`, a protobuf of conversation summaries). When the
app crashes during a rewrite of that index, the file is left truncated to
zeros. **Your chats are not deleted** — the app just no longer knows they
exist. Rebuilding the index restores them.

## Recovery

Close Antigravity completely (check the tray), then:

```bat
:: 1. see whether recovery is needed and what can be recovered
node antigravity_index_recover.js check

:: 2. copy everything relevant to a safe place (never skipped, always do this)
node antigravity_index_recover.js backup

:: 3. build + verify + install the reconstructed index
node antigravity_index_recover.js rebuild --apply
```

`rebuild` without `--apply` is a dry run: it builds the index and verifies it
but writes only to a `.recovered` file. The tool refuses to install anything
while Antigravity is running, keeps the damaged file (renamed, never deleted),
and skips conversations whose content files are themselves zeroed (nothing to
show there).

A Python port (`antigravity_index_recover.py`, standard library only) offers
the same commands:

```bat
py antigravity_index_recover.py check
py antigravity_index_recover.py backup
py antigravity_index_recover.py rebuild --apply
```

> Both implementations were validated against a real recovery (68
> conversations restored, Antigravity 2026-09): `check`, `backup` and
> `rebuild --apply` all confirmed end-to-end with identical results.

Requires Node.js ≥ 22.5 (for the built-in `node:sqlite` module; on Node 22.x
prefix with `--experimental-sqlite` if needed), or Python 3.8+ for the port.
Windows only (paths and `tasklist` are Windows-specific).

## What the tool reconstructs, and where it gets it

For every conversation file it extracts:

| Index field | Source |
|---|---|
| conversation UUID | file name |
| title | first user message (`step_type 14`) inside the `.db`; for legacy `.pb` files, the mirrors in `state.vscdb` or the `brain/<id>/task.md` heading |
| timestamps | first/last step metadata; falls back to file times |
| trajectory id | `trajectory_meta` table |
| workspace URI | `trajectory_metadata_blob` |
| **project id** | same blob (field 18); the app groups chats by project, and entries without this field are dropped by the UI |
| session id | same blob (field 3) |

If a workspace has no project in `~/.gemini/config/projects/`, the tool
registers one there (same JSON schema the app uses); conversations without a
workspace go to the built-in `outside-of-project` bucket.

## Index format reference (reverse-engineered, Sep 2026)

`agyhub_summaries_proto.pb` = repeated `Entry` (field 1, length-delimited):

```
Entry {
  1: string  conversation UUID (= .db file name)
  2: Summary {
       1: string   title
       2: varint   step count
       3: Timestamp { 1: seconds, 2: nanos }   // last update
       4: string  trajectory UUID              // trajectory_meta.trajectory_id
       5: varint  = 1
       7: Timestamp                              // created
       9: Workspace { 1: "file:///d%3A/...", 3: {} }
      10: Timestamp                              // last opened
      15: { 7: Timestamp }
      16: varint  // read cursor; app writes step_count - 2
      17: Project {
            1: Workspace { 1: uri, 3: {} }
            2: Timestamp   // created
            3: string      // session UUID (trajectory_metadata_blob f3)
            6: string      // conversation UUID
            7: string      // workspace URI with drive colon as %3A
           18: string      // PROJECT UUID - entries without it are ignored
          }
      22: varint  = 4
     }
}
```

Payloads inside the conversation databases are stored two ways — plain binary
protobuf *and* decimal-text (`"10,165,1,..."` = comma-separated byte values).
The tool tries both. Old `.pb` conversations are encrypted, but their summaries
survive inside `state.vscdb` (`ItemTable` keys
`antigravityUnifiedStateSync.trajectorySummaries` /
`unifiedStateSync.trajectorySummaries`), where each entry is
`{1: convId, 2: {1: base64(summary)}}`.

## If it fails after an app update

Google changes things. If `rebuild` produces an index the app ignores:

1. `check` first — is the file really the problem?
2. Create **one throwaway conversation** in Antigravity so the app itself
   writes a fresh, valid index file.
3. Diff that fresh file against the format above to see what changed, and
   open an issue here with your findings (remove any personal data first).

## Known unrecoverable cases

- Conversation files that are themselves all zeros (pre-existing corruption;
  the tool reports them and skips them).
- Conversations whose `.db`/`.pb` was deleted before the crash.

## Reporting the bug upstream

This crash is an Antigravity bug (a failed index rewrite should not zero the
file). Please also report it via **Help → Report issues** in Antigravity or at
Google's Antigravity issue tracker, and mention that the conversation content
survives — only the index is lost.

## License

MIT (or your choice). Provided as-is, with no warranty.
