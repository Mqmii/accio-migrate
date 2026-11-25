# accio-migrate

A **single-file** tool for migrating Accio chat history from one account to another.
It **automatically discovers** account IDs and identity formats — no need to memorize emails/IDs.

> **Disclaimer:** Unofficial tool; not affiliated with or endorsed by Accio. Use at your
> own risk. Moving corporate chat data into a personal account may violate company policy.

```
accio_migrate.py   # single file, Python stdlib only
_selftest.py       # 81 checks with synthetic data — does NOT touch real data
_inventory.py      # read-only inventory: which chats are in which account, plus backups
README.md          # this file
```

> **For other agents:** Follow Section 0 step by step. Do not touch the pitfalls in
> Section 7 — the behaviors there are verified by synthetic tests.

---

## 0. Quick start (5 minutes)

```powershell
cd "<this folder>"

# 1) Verification: 81/81 passed. If it fails, DO NOT USE THE TOOL.
python _selftest.py

# 2) List accounts (read-only)
python accio_migrate.py accounts

# 3) SHOW what will be migrated (read-only, writes nothing)
python accio_migrate.py plan -f SOURCE -t DEST

# 4) CLOSE the Accio client, then:
python accio_migrate.py apply -f SOURCE -t DEST

# 5) Verify afterwards (apply already does this; safe to run again)
python accio_migrate.py verify -t DEST -f SOURCE
```

`SOURCE` / `DEST` can be one of the following:

| format | example | note |
|---|---|---|
| account ID | `1000000001` | `~/.accio/accounts/` folder name |
| index number in the list | `0`, `1` | **can change** — the most recently used comes first |
| name/email fragment | `example`, `a****@example.com` | must match exactly one entry |

Always confirm with `accounts` first; don't rely on index numbers.

---

## 1. Commands

| command | what it does | writes? |
|---|---|---|
| `accounts [--json]` | accounts: id, name, email, accioId, **unique chat** count, size | no |
| `plan -f -t` | files/DB/size to be migrated + sample transformations | no |
| `backup -a H` | full backup of the account | backup folder |
| `apply -f -t` | backup → migrate → verify | **yes** |
| `export -a H -o F` | portable package (`.accioexport` = zip) | file |
| `import -i F -t H` | apply the package to the destination (backup + verify) | **yes** |
| `verify -t H [-f K]` | stale-identity remnants, `quick_check`, chat count | no |
| `_inventory.py` | detailed + backup inventory of both accounts (script) | no |

Common parameters:

```
--suffix-len N     account suffix length  (default 0 = auto-detect from data)
--accio-id ...      source accioId  (required if remembered-accounts.json is missing)
--accio-id-to ...   destination accioId
--home PATH         Accio data vault (default ~/.accio; $ACCIO_HOME is also read)
```

`apply` / `import` flags:

```
-y, --yes       do not ask for confirmation
--no-backup     does NOT take a backup — and beware: it also skips the confirmation prompt!
--overwrite     overwrite same-named files at the destination (default: NO)
--force         continue even while Accio is running (risk is on the user)
```

> `--home` can be placed **before or after the subcommand** — both work
> (subcommands use `default=argparse.SUPPRESS`; see Section 7).

---

## 2. Exit codes

| code | meaning |
|---|---|
| `0` | success / verification clean |
| `1` | usage error, account/package not found, user cancelled, Accio open without `--force` |
| `2` | operation finished **but verification has warnings** — read the output |
| `130` | Ctrl+C |

If you use it inside a script, treat any code other than `0` as an error.

---

## 3. File/ID locations

```
~/.accio/accounts/<accountId>/
    conversations/dm/<CID>.{json,branch.jsonl,branch.leaf,message_1.jsonl,seq.json}
    conversations/team/...
    conversations/conv-index.db        # conversations table
    msg-archive/archive.db             # archive_meta + u_ai_acciowork_<accioId>_<ts>
    agents/<DID-...>/agent-core|sessions|skills|permissions|tools|runtime
    agents/MID-<NN><last6>U<ts>-.../
    agents/sqlite/index.db             # sessions, messages, messages_fts, message_vecs(vec0)
    tasks/<CID>/...
    skills/ channels/ connectors/ plugins/ permissions/ ...   # NOT MIGRATED

remembered-accounts.json  (accountId <-> name/email/accioId):
    Windows : %APPDATA%\Accio\
    macOS   : ~/Library/Application Support/Accio/
    Linux   : $XDG_CONFIG_HOME/Accio  or  ~/.config/Accio/
    backup  : ~/.accio/
```

One chat = **multiple files** (all with the same `<CID>` prefix). The `chat`
column in the `accounts` output is the **number of unique CIDs**, not the file count.

---

## 4. What is migrated, which identities are rewritten

**Migrated surface** = `conversations/`, `msg-archive/`, `agents/` (excluding `runtime/`),
`tasks/`, `subagent-sessions/` + all `*.db` found by `discover_dbs()`.

**Four independent identities** are transformed (all pass through the same `Rewriter`):

| # | identity | example |
|---|---|---|
| 1 | account suffix + `U` (CID/MID/DID file and content names) | `…000001U…` → `…000002U…` |
| 2 | `storageKey` (JSON) / `storage_key` (DB column) | account ID |
| 3 | `accioId` (request identity **and dynamic table name**) | `4500000000001` → `4500000000002` |
| 4 | agent `DID-…` | source DID → destination DID |

Additional rules:

- **Digit boundaries:** account/accio IDs are replaced with `(?<!\d)…(?!\d)`; even if
  they appear inside an epoch timestamp, they are **not corrupted**. The account-suffix
  rule deliberately has **no left boundary** (CID `79000001U` must become `79000002U`).
- **Automatic suffix length:** the common suffix length of the two account IDs in the
  `…<digits>U` pattern is found by voting (in the example: **6**). You can pin it with
  `--suffix-len`.
- **Agent matching:** if there is **exactly 1 DID** in the source and **exactly 1 DID**
  in the destination, they are matched 1:1 (the chat attaches to the existing agent in
  the destination; no second agent is created). Otherwise the DID is copied as-is.
- **Agent-core:** if the file already exists in the destination it is **left untouched**
  (the destination's memory is preserved); missing files are added. `agents/*/runtime/`
  is never migrated (lock/state files).
- **Not migrated:** `skills/`, `channels/`, `connectors/`, `plugins/`, `permissions/`,
  `automations/`, `workspaces/`, `artifacts/`, `archive/`, `debug/`, `mcp-servers/`,
  `pairings/`, `agent-hub/`, `agent-creation/`, `web-builder/`, `.accio/`,
  settings, and identity files.

---

## 5. Safety, idempotency, rollback

- `apply`/`import` first takes a **full backup of the destination account**:
  `~/.accio/backups/<YYYYMMDD_HHMMSS>_<accountId>/` (+ `.meta.json`)
- **The source account is never written** (it is opened read-only only).
- **Idempotent:** on a second run, no files/rows are added (`existing`/`skipped`
  counters increase, `inserted`/`copied` stay constant). It is safe to finish a broken
  run by running it again.
- If the Accio DB is locked, the tool **fails immediately with an error instead of
  waiting** (close the application first to resolve this).
- Automatic `verify` after migration: stale-identity remnants (text + DB scan),
  `PRAGMA quick_check`, and row counts.
- **Rollback** (while Accio is closed; since the backup is complete, also preserve the
  current state):

```powershell
$acc = "$env:USERPROFILE\.accio\accounts\<accountId>"
$bk  = "$env:USERPROFILE\.accio\backups\<timestamp>_<accountId>"
# first set aside the current (post-migration) state, then restore the backup
Rename-Item -Path $acc -NewName "<accountId>.after-migration"
Copy-Item -Recurse -Path $bk -Destination $acc
Remove-Item -Path "$acc\.meta.json" -ErrorAction SilentlyContinue
```

> Moving corporate data into a personal account may violate company policy;
> remind the user before migrating.

---

## 6. Architecture

![Architecture diagram: accounts flow through resolve_account and build_rewriter to Rewriter, which branches into copy_surface (files) and handle_db (SQLite), both converging on verify](docs/architecture.svg)

- **Single transformation source:** `Rewriter.__call__` applies the same rules to both
  file content and row values → files and DB never diverge. `non-str` values
  (None/int/bytes) are returned unchanged.
- **Two DB strategies:**
  - destination DB **missing** → the file + `-wal` are copied and transformed in place
    (`ALTER TABLE … RENAME` + row `UPDATE`). **The vec0/FTS schema is preserved.**
  - destination DB **exists** → row-level merge; new tables are created from the source
    DDL (with transformed names), deduplicated with `INSERT OR IGNORE` + the set of
    occupied PK keys. FTS is rebuilt last from `messages`.
- **Discovery:** which DBs are relevant is determined by `discover_dbs()` (`*.db` under
  `INCLUDE_TREES`) → if a new Accio version adds tables/DBs, no code change is needed.
- **Format:** package = zip + `manifest.json` (`source.account_id`, `accio_id`,
  `suffix_len`, `dids`, file list + sha256). `import` performs **fresh discovery**
  from the manifest; the destination account is hard-coded nowhere.

---

## 7. Known pitfalls (DO NOT REGRESS)

1. **`--home` must use `default=argparse.SUPPRESS` in subcommands.**
   A normal subcommand argument **overrides** the top-level `--home X` value (argparse
   copies sub-parser defaults into the parent namespace). If SUPPRESS is removed, the
   tool could accidentally operate on the real data directory. The guard in Section 8
   catches this too.

2. **`PRAGMA table_info` column order:** `(cid, name, type, notnull, dflt_value, pk)`
   → **pk = `r[5]`**. `r[4]` = `dflt_value` and **arrives as a string** (`'0'` is truthy);
   if mistaken for pk, `DEFAULT 0` columns become dedup keys and rows are silently
   skipped. `accio_migrate.py::_table_info` handles this.

3. **FTS5 `delete-all`** works only on *contentless / external-content* tables. It errors
   on a normal fts5 table → `_rebuild_fts` first tries `DELETE FROM messages_fts`, then
   falls back to `delete-all`.

4. **The `vec0` module is not in the stdlib.** Opening/enumerating the DB *without
   querying it* is safe; `SELECT … FROM message_vecs` errors. Therefore that table is
   not handled manually in either cloning or merging; `_classify_tables` **filters it
   and its shadow tables out**.

5. **`--no-backup` also skips the confirmation prompt.** Be careful when telling the user.

---

## 8. Self-test and inventory

```powershell
python _selftest.py      # 81/81 — synthetic data, temp folder, does NOT touch the real account
python _inventory.py     # read-only: chat/title/CID/row counts + backups
```

`_selftest.py` covers: CID/MID/storageKey/accioId/DID transformations, epoch timestamp
preservation, automatic suffix detection, file copying + idempotency,
`clone_and_rewrite` (vec0 preserved, FTS rebuilt), `merge_into` (dynamic table naming +
idempotent), `discover_dbs`, and **end-to-end `export → import` at the CLI level**
(fake `--home` + synthetic `remembered-accounts.json` + safety guard).

> Run the test as the **first thing** on your machine. If there is any `FAIL`, do not
> run the tool.

---

## 9. Privacy

This repository contains no real account IDs, emails, chat titles/content, usernames,
or backup paths. The example identities are entirely synthetic. The tool operates only
on the local Accio data vault; it sends no data to the network.
