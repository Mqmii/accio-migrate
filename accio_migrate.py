#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
accio_migrate.py -- Tool for transferring chats between Accio accounts / backup / import-export.

Transferred surface (chat surface):
    conversations/{dm,team}/**, conversations/conv-index.db,
    msg-archive/archive.db, agents/** (except sqlite/ and runtime/), tasks/**,
    subagent-sessions/**

Not transferred (intentionally): skills, connectors, channels, plugins, permissions,
automations, workspaces, artifacts, web-builder, debug, settings, identity files.

Commands:
    accounts              list accounts
    plan     -f S -t D    SHOW what would be transferred (does not write)
    backup   -a A         full backup of the account
    apply    -f S -t D    transfer (back up first, then verify)
    export   -a A -o F    produce a portable package (for another machine)
    import   -i F -t D    apply the package to the target account
    verify   -t D [-f S]  verification afterwards

Note: Close the Accio client application before running.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- constants

FORMAT_NAME = "accio-export"
FORMAT_VERSION = 1
DEFAULT_SUFFIX_LEN = 6
INCLUDE_TREES = ("conversations", "msg-archive", "agents", "tasks",
                 "subagent-sessions")
SKIP_FILE_NAMES = {".sink-migrated"}
AGENT_RUNTIME_SEG = "runtime"      # agents/<agent>/runtime -> state file, skipped during transfer
MAX_TEXT_BYTES = 64 * 1024 * 1024
SIDECAR_SUFFIXES = ("-wal", "-shm")

TEXT_DECODERS = ("utf-8", "utf-8-sig")


# ---------------------------------------------------------------- small utils

def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"  [!] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"  [x] {msg}", file=sys.stderr, flush=True)


def ok(msg: str) -> None:
    print(f"  [+] {msg}", flush=True)


def heading(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{x:.1f}{unit}" if unit != "B" else f"{int(x)}B"
        x /= 1024
    return f"{x:.1f}GB"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def read_text(data: bytes) -> Optional[str]:
    for enc in TEXT_DECODERS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


# ---------------------------------------------------------------- account meta

def accio_home(override: Optional[str] = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("ACCIO_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".accio"


def remembered_accounts_path() -> Optional[Path]:
    """Find the remembered-accounts.json location for the current platform."""
    candidates: List[Path] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Accio" / "remembered-accounts.json")
    else:
        candidates.append(Path.home() / "Library" / "Application Support"
                          / "Accio" / "remembered-accounts.json")
        xdg = os.environ.get("XDG_CONFIG_HOME")
        candidates.append((Path(xdg) if xdg else Path.home() / ".config")
                          / "Accio" / "remembered-accounts.json")
    candidates.append(Path.home() / ".accio" / "remembered-accounts.json")
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_account_meta() -> Dict[str, dict]:
    """accountId -> {name, email, accio_id, level, team, login_at}"""
    path = remembered_accounts_path()
    if not path:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    for acc_id, node in (raw or {}).items():
        auth = node.get("auth") or {}
        out[str(acc_id)] = {
            "name": (auth.get("name") or "").strip(),
            "email": auth.get("desensitizedEmail") or "",
            "accio_id": str(auth.get("accioId") or ""),
            "level": auth.get("userLevel") or "",
            "team": auth.get("activeTeam"),
            "login_at": node.get("loginAt") or 0,
        }
    return out


_ACCIO_TOKEN_RE = re.compile(rb"AI_Accio[A-Za-z]*_(\d{10,20})_")


def detect_accio_id(account_dir: Path) -> str:
    """Scan the data for accioId if there is no metadata file."""
    for rel in ("conversations", "msg-archive", "agents"):
        root = account_dir / rel
        if not root.exists():
            continue
        for f in root.rglob("*"):
            if not f.is_file() or f.stat().st_size > 8 * 1024 * 1024:
                continue
            if f.suffix.lower() not in (".json", ".jsonl", ".jsonc", ".db",
                                        ".md", ".leaf"):
                continue
            try:
                data = f.read_bytes()
            except Exception:
                continue
            m = _ACCIO_TOKEN_RE.search(data)
            if m:
                return m.group(1).decode()
            # Try searching SQLite content directly
            if f.suffix == ".db":
                try:
                    con = sqlite3.connect(
                        f"file:{f.as_posix()}?mode=ro", uri=True)
                    for (name,) in con.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table'"):
                        m = _ACCIO_TOKEN_RE.search(name.encode())
                        if m:
                            con.close()
                            return m.group(1).decode()
                    con.close()
                except Exception:
                    pass
    return ""


# ---------------------------------------------------------------- account model

class Account:
    def __init__(self, acc_id: str, path: Path, meta: Optional[dict] = None):
        self.id = acc_id
        self.path = path
        meta = meta or {}
        self.name = meta.get("name") or ""
        self.email = meta.get("email") or ""
        self.accio_id = meta.get("accio_id") or ""
        self.level = meta.get("level") or ""
        self.login_at = meta.get("login_at") or 0

    # -- statistics (read-only)
    @property
    def conversation_count(self) -> int:
        """Number of distinct conversations (not the file count).

        A single chat creates multiple files:
          <CID>.json, <CID>.branch.jsonl, <CID>.branch.leaf,
          <CID>.message_1.jsonl, <CID>.seq.json
        -> the prefix (up to the first dot) is the unique CID.
        """
        ids = set()
        for sub in ("dm", "team"):
            d = self.path / "conversations" / sub
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or f.name in SKIP_FILE_NAMES:
                    continue
                if is_db_related(f.name):
                    continue
                ids.add(f.name.split(".")[0])
        return len(ids)

    @property
    def agent_ids(self) -> List[str]:
        d = self.path / "agents"
        if not d.is_dir():
            return []
        return sorted(x.name for x in d.iterdir()
                      if x.is_dir() and x.name.startswith("DID-"))

    @property
    def mid_ids(self) -> List[str]:
        d = self.path / "agents"
        if not d.is_dir():
            return []
        return sorted(x.name for x in d.iterdir()
                      if x.is_dir() and x.name.startswith("MID-"))

    @property
    def size_bytes(self) -> int:
        total = 0
        for tree in INCLUDE_TREES:
            root = self.path / tree
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total

    @property
    def last_modified(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @property
    def label(self) -> str:
        bits = [b for b in (self.name, self.email) if b]
        return " ".join(bits) if bits else "(unnamed)"


def list_accounts(home: Path) -> List[Account]:
    root = home / "accounts"
    if not root.is_dir():
        return []
    meta = load_account_meta()
    out = []
    for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime,
                    reverse=True):
        if not d.is_dir() or d.name == "guest":
            continue
        a = Account(d.name, d, meta.get(d.name))
        if not a.accio_id:
            a.accio_id = detect_accio_id(d)
        out.append(a)
    return out


def resolve_account(home: Path, spec: str) -> Account:
    """spec: full id | index (number in the accounts list) | name/email fragment."""
    accounts = list_accounts(home)
    if not accounts:
        raise SystemExit(f"no accounts found: {home / 'accounts'}")
    spec = spec.strip()
    for a in accounts:
        if a.id == spec:
            return a
    if spec.isdigit():
        idx = int(spec)
        if 0 <= idx < len(accounts):
            return accounts[idx]
    low = spec.lower()
    hits = [a for a in accounts
            if low in a.id.lower() or low in a.name.lower()
            or low in a.email.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"no account found for '{spec}'. "
                         f"List them with `accounts`.")
    raise SystemExit(f"'{spec}' is ambiguous ({len(hits)} matches): "
                     + ", ".join(f"{a.id}({a.label})" for a in hits))


# ---------------------------------------------------------------- suffix detect

def _tokens_with_u(names: Iterable[str]) -> List[str]:
    """Digit tokens of the form 'DigitsU' in folder/file names."""
    out = []
    for n in names:
        for m in re.finditer(r"(\d{4,})U", n):
            out.append(m.group(1))
    return out


def detect_suffix_len(account_dir: Path, src_acct: str,
                      dst_acct: str) -> int:
    """Detect the 'last N digits of the account + U' rule in IDs from the data."""
    names: List[str] = []
    for tree in ("agents", "conversations", "tasks"):
        root = account_dir / tree
        if not root.exists():
            continue
        for f in root.rglob("*"):
            names.append(f.name)
    tokens = _tokens_with_u(names)
    if not tokens or not src_acct:
        return DEFAULT_SUFFIX_LEN

    votes: Dict[int, int] = {}
    for t in tokens:
        best = 0
        for k in range(1, min(len(src_acct), len(t)) + 1):
            if t.endswith(src_acct[-k:]) and src_acct.endswith(src_acct[-k:]):
                best = k
        if best and (not dst_acct or len(dst_acct) >= best):
            votes[best] = votes.get(best, 0) + 1
    if not votes:
        return DEFAULT_SUFFIX_LEN
    # most common vote; on a tie, the smaller (more conservative) N
    top = max(votes.values())
    cands = [k for k, v in votes.items() if v == top]
    return min(cands)


# ---------------------------------------------------------------- rewriter

class Rewriter:
    """Single source of truth for all identity transformations.

    Text and SQLite apply the same rules:
      - DID match                           (plain copy)
      - accioId  (digit-bounded)            4500000000001 -> 4500000000002
      - accountId (digit-bounded)           1000000001    -> 2000000002
      - account suffix + 'U' (unbounded left) 000001U     -> 000002U
    The digit boundary prevents an account ID from shifting into an epoch timestamp.
    """

    def __init__(self, src_acct: str = "", dst_acct: str = "",
                 src_accio: str = "", dst_accio: str = "",
                 suffix_len: int = 0,
                 did_map: Optional[Dict[str, str]] = None):
        self.src_acct = src_acct or ""
        self.dst_acct = dst_acct or ""
        self.src_accio = src_accio or ""
        self.dst_accio = dst_accio or ""
        self.suffix_len = int(suffix_len or 0)
        self.did_map = dict(did_map or {})
        self.src_did = list(self.did_map.keys())

        self._regex: List[Tuple[re.Pattern, str]] = []
        self._plain: List[Tuple[str, str]] = []

        # priority order: most specific -> most general
        for s, d in sorted(self.did_map.items()):
            if s and d and s != d:
                self._plain.append((s, d))
        if self.src_accio and self.dst_accio and self.src_accio != self.dst_accio:
            self._regex.append((re.compile(r"(?<!\d)%s(?!\d)"
                                           % re.escape(self.src_accio)),
                                self.dst_accio))
        if self.src_acct and self.dst_acct and self.src_acct != self.dst_acct:
            self._regex.append((re.compile(r"(?<!\d)%s(?!\d)"
                                           % re.escape(self.src_acct)),
                                self.dst_acct))
        if self.suffix_len and self.src_acct and self.dst_acct:
            sfx_s = self.src_acct[-self.suffix_len:]
            sfx_d = self.dst_acct[-self.suffix_len:]
            if sfx_s and sfx_d and sfx_s != sfx_d:
                # we deliberately leave the left side unbounded: '79'+sfx == '79'+newSfx
                self._plain.append((sfx_s + "U", sfx_d + "U"))

    @property
    def active(self) -> bool:
        return bool(self._plain or self._regex)

    @property
    def is_identity(self) -> bool:
        return not self.active

    def __call__(self, value):
        # Also called as a SQLite function -> non-str values pass through unchanged
        if value is None or not isinstance(value, str) or not value:
            return value
        s = value
        for pat, repl in self._regex:
            s = pat.sub(repl, s)
        for a, b in self._plain:
            s = s.replace(a, b)
        return s

    def describe(self) -> List[str]:
        lines = []
        for s, d in sorted(self.did_map.items()):
            lines.append(f"agent  {s}  ->  {d}")
        if self.src_accio and self.src_accio != self.dst_accio:
            lines.append(f"accio  {self.src_accio}  ->  "
                         f"{self.dst_accio or '(UNKNOWN, left untouched)'}")
        if self.src_acct and self.src_acct != self.dst_acct:
            lines.append(f"account {self.src_acct}  ->  {self.dst_acct}")
        if self.suffix_len and self.src_acct and self.dst_acct:
            s_s = self.src_acct[-self.suffix_len:]
            s_d = self.dst_acct[-self.suffix_len:]
            if s_s != s_d:
                lines.append(f"suffix {s_s}U  ->  {s_d}U   "
                             f"(last {self.suffix_len} digits, for CID/MID)")
        return lines or ["(no changes)"]

    def leftover_needles(self) -> List[str]:
        """Source tokens that should not remain in the target (for verification)."""
        out = []
        out.extend(self.src_did)
        if self.src_accio and self.src_accio != self.dst_accio:
            out.append(self.src_accio)
        if self.src_acct and self.src_acct != self.dst_acct:
            out.append(self.src_acct)
        if self.suffix_len and self.src_acct and self.dst_acct:
            s_s = self.src_acct[-self.suffix_len:]
            s_d = self.dst_acct[-self.suffix_len:]
            if s_s != s_d:
                out.append(s_s + "U")
        return out


def build_rewriter(src: Account, dst: Account,
                   suffix_len: Optional[int] = None) -> Rewriter:
    if suffix_len in (None, 0):
        slen = detect_suffix_len(src.path, src.id, dst.id)
    else:
        slen = int(suffix_len)

    # Agent mapping: if there is a single DID in the source and a DID in the target, use a 1:1 map.
    # This way chats attach to the existing agent in the target and no second agent is created.
    src_dids = src.agent_ids
    dst_dids = dst.agent_ids
    did_map: Dict[str, str] = {}
    if len(src_dids) == 1 and len(dst_dids) == 1:
        did_map[src_dids[0]] = dst_dids[0]

    dst_accio = dst.accio_id or ""
    if not dst_accio and src.accio_id:
        warn(f"target accioId is unknown ({dst.id}); accio tokens "
             f"will not be converted. You can supply it with `--accio-id-to`.")

    return Rewriter(src_acct=src.id, dst_acct=dst.id,
                    src_accio=src.accio_id, dst_accio=dst_accio,
                    suffix_len=slen, did_map=did_map)


# ---------------------------------------------------------------- db helpers

def discover_dbs(root: Path) -> List[str]:
    """Relative paths of *.db files in the chat surface."""
    out = []
    for tree in INCLUDE_TREES:
        base = root / tree
        if not base.exists():
            continue
        for f in base.rglob("*.db"):
            if f.is_file():
                out.append(f.relative_to(root).as_posix())
    return sorted(out)


def is_db_related(rel: str) -> bool:
    name = Path(rel).name
    return name.endswith(".db") or any(name.endswith(s) for s in SIDECAR_SUFFIXES)


def _table_kind(sql: str) -> str:
    u = (sql or "").upper()
    if "USING FTS" in u:
        return "fts"
    if re.search(r"USING\s+\w+", u):
        return "virtual"
    return "table"


def _classify_tables(con: sqlite3.Connection) -> Tuple[List[str], List[str]]:
    """(only simple user tables, fts tables) -> shadow/virtual ones are filtered out."""
    rows = con.execute(
        "SELECT name, COALESCE(sql,'') FROM sqlite_master "
        "WHERE type='table'").fetchall()
    kinds = {n: _table_kind(s) for n, s in rows}
    fts = [n for n, k in kinds.items() if k == "fts"]
    virt = [n for n, k in kinds.items() if k == "virtual"]
    simple, shadows = [], []
    for n, k in kinds.items():
        if n.startswith("sqlite_"):
            shadows.append(n)
            continue
        if k in ("fts", "virtual"):
            shadows.append(n)
            continue
        if any(n.startswith(p + "_") for p in list(fts) + list(virt)):
            shadows.append(n)
            continue
        simple.append(n)
    return sorted(simple), sorted(fts)


def _table_info(con: sqlite3.Connection, schema: str, table: str
                ) -> List[Tuple[str, str, int, int]]:
    """List of (name, decl, notnull, pk).

    PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
    -> pk is at index 5. dflt_value comes as a STRING ('0' = truthy);
       if mixed up, 'DEFAULT 0' columns are mistaken for pk and the
       dedup key breaks.
    """
    cur = con.execute(f'PRAGMA {schema}.table_info("{table}")')
    return [(r[1], r[2] or "", r[3] or 0, r[5] or 0) for r in cur.fetchall()]


def _has_autopk(ddl: str, cols: Sequence[Tuple[str, str, int, int]]) -> bool:
    u = (ddl or "").upper()
    if "AUTOINCREMENT" not in u:
        return False
    for name, decl, _nn, pk in cols:
        if pk and "INTEGER" in decl.upper() and "PRIMARY KEY" in decl.upper():
            return True
    return False


def _create_like(con: sqlite3.Connection, ddl: str, new_name: str) -> None:
    body = ddl[ddl.index("("):]                      # column list after the first '('
    con.execute(f'CREATE TABLE IF NOT EXISTS "{new_name}" {body}')


def _rebuild_fts(con: sqlite3.Connection) -> None:
    """Rebuild messages_fts from the messages table."""
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "messages" not in names or "messages_fts" not in names:
        return
    try:
        cols = [r[1] for r in con.execute(
            'PRAGMA table_info("messages_fts")')]
        cols = [c for c in cols if c and c != "rowid"]
        if not cols:
            return
        # normal fts5 (has its own content) -> DELETE.
        # contentless/external-content fts5 -> 'delete-all' is required.
        try:
            con.execute("DELETE FROM messages_fts")
        except Exception:
            con.execute("INSERT INTO messages_fts(messages_fts) "
                        "VALUES('delete-all')")
        collist = ", ".join(f'"{c}"' for c in cols)
        con.execute(
            f"INSERT INTO messages_fts(rowid, {collist}) "
            f"SELECT rowid, {collist} FROM messages")
    except Exception as e:
        warn(f"Could not rebuild FTS: {e}")


def _checkpoint(con: sqlite3.Connection) -> None:
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open read-only; fall back to a temporary copy on WAL issues."""
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except Exception:
        tmp = tempfile.mkdtemp(prefix="accio_ro_")
        dst = Path(tmp) / path.name
        shutil.copy2(path, dst)
        for sfx in SIDECAR_SUFFIXES:
            side = Path(str(path) + sfx)
            if side.exists():
                shutil.copy2(side, Path(str(dst) + sfx))
        return sqlite3.connect(dst)


def clone_and_rewrite(src_db: Path, dst_db: Path, rw: Rewriter) -> Dict[str, int]:
    """Target DB missing: copy the file (including vector tables), transform in place."""
    stat = {"renamed": 0, "updated": 0}
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_db, dst_db)
    for sfx in SIDECAR_SUFFIXES:
        side = Path(str(src_db) + sfx)
        if side.exists():
            shutil.copy2(side, Path(str(dst_db) + sfx))

    con = sqlite3.connect(dst_db, timeout=30)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        if rw.is_identity:
            _checkpoint(con)
            return stat

        simple, _fts = _classify_tables(con)

        # 1) table name transformation (example: u_ai_acciowork_<accioId>_...)
        rows = con.execute(
            "SELECT name, COALESCE(sql,'') FROM sqlite_master "
            "WHERE type='table'").fetchall()
        renames = [(n, rw(n)) for n, _s in rows
                   if not n.startswith("sqlite_") and rw(n) != n]
        existing = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for old, new in renames:
            if new in existing:
                continue
            con.execute(f'ALTER TABLE "{old}" RENAME TO "{new}"')
            stat["renamed"] += 1
        if renames:
            con.commit()
            simple, _fts = _classify_tables(con)

        # 2) row content transformation (all columns; non-str values pass through unchanged)
        for tbl in simple:
            try:
                cols = [r[1] for r in con.execute(
                    f'PRAGMA table_info("{tbl}")')]
                if not cols:
                    continue
                collist = ", ".join(f'"{c}"' for c in cols)
                placeholders = ", ".join("?" * len(cols))
                setlist = ", ".join(f'"{c}"=?' for c in cols)
                cur = con.execute(f"SELECT rowid, {collist} FROM \"{tbl}\"")
                batch = []
                for row in cur.fetchall():
                    rid, values = row[0], row[1:]
                    newvals = tuple(rw(v) for v in values)
                    if newvals != values:
                        batch.append((*newvals, rid))
                if batch:
                    con.executemany(
                        f'UPDATE "{tbl}" SET {setlist} WHERE rowid=?', batch)
                    stat["updated"] += len(batch)
            except Exception as e:
                warn(f"Could not update {tbl}: {e}")
        con.commit()

        _rebuild_fts(con)
        con.commit()
        _checkpoint(con)
    finally:
        con.close()
    return stat


def merge_into(src_db: Path, dst_db: Path, rw: Rewriter) -> Dict[str, int]:
    """Target DB exists: read row by row from the source, transform, add to the target."""
    stat = {"created_tables": 0, "inserted": 0, "skipped": 0, "updated": 0}
    src = _open_ro(src_db)
    try:
        dst = sqlite3.connect(dst_db, timeout=30)
    finally:
        pass
    try:
        dst.execute("PRAGMA foreign_keys=OFF")
        src_simple, src_fts = _classify_tables(src)
        dst_names = {r[0] for r in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        src_ddl = {r[0]: r[1] for r in src.execute(
            "SELECT name, COALESCE(sql,'') FROM sqlite_master "
            "WHERE type='table'")}

        # create tables missing in the target (including fts; virtual tables are left out)
        for tbl in src_simple + src_fts:
            new_name = rw(tbl)
            if new_name not in dst_names:
                _create_like(dst, src_ddl.get(tbl, ""), new_name)
                stat["created_tables"] += 1
                dst_names.add(new_name)
        dst.commit()

        # for messages: skip sessions that already exist in the target
        dst_session_keys: set = set()
        if "sessions" in dst_names:
            dst_session_keys = {r[0] for r in dst.execute(
                "SELECT session_key FROM sessions")}

        for tbl in src_simple:
            new_tbl = rw(tbl)
            cols = _table_info(src, "main", tbl)
            if not cols:
                continue
            ddl = src_ddl.get(tbl, "")
            autopk = _has_autopk(ddl, cols)
            names = [c[0] for c in cols]
            ins_names = [c[0] for c in cols
                         if not (autopk and c[3]
                                 and "INTEGER" in c[1].upper())]
            quoted = ", ".join(f'"{n}"' for n in ins_names)
            placeholders = ", ".join("?" * len(ins_names))
            insert_sql = (f'INSERT OR IGNORE INTO "{new_tbl}" ({quoted}) '
                          f"VALUES ({placeholders})")

            # existing rows (dedup key)
            existing: set = set()
            all_rows_exist = False
            try:
                rowcount = dst.execute(
                    f'SELECT count(*) FROM "{new_tbl}"').fetchone()[0]
                all_rows_exist = rowcount > 200000
            except Exception:
                rowcount = 0
            if not all_rows_exist:
                if autopk:
                    # automatic PK -> dedup on the full row (excluding the PK)
                    other = [c[0] for c in cols
                             if not (c[3] and "INTEGER" in c[1].upper())]
                    if other:
                        oq = ", ".join(f'"{n}"' for n in other)
                        existing = {tuple(r) for r in dst.execute(
                            f'SELECT {oq} FROM "{new_tbl}"')}
                else:
                    pks = [c[0] for c in cols if c[3]] or names
                    pq = ", ".join(f'"{n}"' for n in pks)
                    existing = {tuple(r) for r in dst.execute(
                        f'SELECT {pq} FROM "{new_tbl}"')}

            cur = src.execute(f'SELECT {", ".join(chr(34)+n+chr(34) for n in names)} FROM "{tbl}"')
            batch: List[tuple] = []
            new_sessions: set = set()
            is_sessions = tbl == "sessions"
            is_messages = tbl == "messages"

            for row in cur.fetchall():
                vals = tuple(rw(v) for v in row)
                by_name = dict(zip(names, vals))

                if is_messages:
                    sk = by_name.get("session_key")
                    if sk and sk not in new_sessions and sk not in dst_session_keys:
                        pass  # session will be added below
                    if sk in dst_session_keys:
                        stat["skipped"] += 1
                        continue

                if is_sessions:
                    sk = by_name.get("session_key")
                    if sk in dst_session_keys:
                        stat["skipped"] += 1
                        continue

                key_names = ([c[0] for c in cols
                              if not (c[3] and "INTEGER" in c[1].upper())
                              and c[0] in by_name] if autopk
                             else [c[0] for c in cols if c[3]] or names)
                key = tuple(by_name.get(k) for k in key_names)
                if existing and key in existing:
                    stat["skipped"] += 1
                    continue

                payload = tuple(by_name.get(n) for n in ins_names)
                batch.append(payload)
                existing.add(key)
                if is_sessions and by_name.get("session_key"):
                    new_sessions.add(by_name["session_key"])

                if len(batch) >= 500:
                    dst.executemany(insert_sql, batch)
                    stat["inserted"] += len(batch)
                    batch = []
            if batch:
                dst.executemany(insert_sql, batch)
                stat["inserted"] += len(batch)
            if is_messages:
                dst_session_keys |= new_sessions
        dst.commit()

        if "messages" in dst_names or any(
                rw(t) == "messages" for t in src_simple):
            _rebuild_fts(dst)
        dst.commit()
        _checkpoint(dst)
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass
    return stat


def handle_db(src_root: Path, dst_root: Path, rel: str, rw: Rewriter,
              force_merge: bool = False) -> Dict[str, int]:
    src_db = src_root / rel
    dst_db = dst_root / rel
    if not src_db.is_file():
        return {}
    if not dst_db.exists() and not force_merge:
        return clone_and_rewrite(src_db, dst_db, rw)
    # the target exists -> we do not replace the file with a backup first; merge row by row
    if not dst_db.exists():
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        dst_db.write_bytes(b"")
    return merge_into(src_db, dst_db, rw)


# ---------------------------------------------------------------- file copy

def _skip_runtime(rel: str) -> bool:
    parts = rel.split("/")
    return len(parts) >= 3 and parts[0] == "agents" and parts[2] == AGENT_RUNTIME_SEG


class CopyReport:
    def __init__(self) -> None:
        self.copied = 0
        self.bytes = 0
        self.rewritten_files = 0
        self.existing = 0
        self.binary = 0
        self.per_tree: Dict[str, Tuple[int, int]] = {}

    def add(self, tree: str, size: int) -> None:
        c, b = self.per_tree.get(tree, (0, 0))
        self.per_tree[tree] = (c + 1, b + size)


def copy_surface(src_root: Path, dst_root: Path, rw: Rewriter,
                 rep: CopyReport, overwrite: bool = False) -> None:
    for tree in INCLUDE_TREES:
        base = src_root / tree
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(src_root).as_posix()
            if is_db_related(rel) or f.name in SKIP_FILE_NAMES:
                continue
            if _skip_runtime(rel):
                continue

            dest_rel = rw(rel)
            dest = dst_root / dest_rel
            if dest.exists() and not overwrite:
                rep.existing += 1
                continue

            try:
                size = f.stat().st_size
            except OSError:
                continue

            data = None
            if size <= MAX_TEXT_BYTES:
                try:
                    data = f.read_bytes()
                except OSError:
                    continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            if data is None or is_binary(data):
                try:
                    shutil.copy2(f, dest)
                    rep.binary += 1
                except OSError as e:
                    warn(f"could not copy {rel}: {e}")
                    continue
                rep.copied += 1
                rep.bytes += size
                rep.add(tree, size)
                continue

            text = read_text(data)
            if text is None:
                try:
                    shutil.copy2(f, dest)
                    rep.binary += 1
                except OSError:
                    continue
                rep.copied += 1
                rep.bytes += size
                rep.add(tree, size)
                continue

            new_text = rw(text) if rw.active else text
            changed = new_text != text
            try:
                dest.write_bytes(new_text.encode("utf-8"))
            except OSError as e:
                warn(f"could not write {dest_rel}: {e}")
                continue
            rep.copied += 1
            rep.bytes += len(new_text.encode("utf-8"))
            if changed:
                rep.rewritten_files += 1
            rep.add(tree, size)


# ---------------------------------------------------------------- backup

def backup_account(home: Path, account: Account) -> Path:
    backup_root = home / "backups"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = backup_root / f"{stamp}_{account.id}"
    n = 1
    while target.exists():
        target = backup_root / f"{stamp}_{account.id}_{n}"
        n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    shutil.copytree(account.path, target,
                    ignore=shutil.ignore_patterns("*.db-shm"))
    meta = {
        "account_id": account.id,
        "accio_id": account.accio_id,
        "name": account.name,
        "email": account.email,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": str(account.path),
    }
    (target / ".meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    ok(f"backup: {target}  ({time.time() - t0:.1f}s)")
    return target


# ---------------------------------------------------------------- locks / app

def app_running() -> bool:
    try:
        import subprocess
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Accio.exe"],
                capture_output=True, text=True, timeout=15)
            return "Accio.exe" in (out.stdout or "")
        out = subprocess.run(["pgrep", "-f", "Accio"],
                             capture_output=True, text=True, timeout=15)
        return bool((out.stdout or "").strip())
    except Exception:
        return False


def assert_dbs_unlocked(dbs: Sequence[Path]) -> None:
    for db in dbs:
        if not db.exists():
            continue
        try:
            con = sqlite3.connect(db, timeout=1)
            con.execute("BEGIN IMMEDIATE").fetchone()
            con.execute("ROLLBACK")
            con.close()
        except sqlite3.OperationalError as e:
            raise SystemExit(
                f"locked: {db}\nClose Accio and try again. ({e})")
        except Exception:
            pass


# ---------------------------------------------------------------- core migrate

def run_migration(src_root: Path, dst_root: Path, rw: Rewriter,
                  overwrite: bool = False) -> Dict[str, object]:
    rep = CopyReport()
    copy_surface(src_root, dst_root, rw, rep, overwrite=overwrite)

    db_stats: Dict[str, Dict[str, int]] = {}
    for rel in discover_dbs(src_root):
        try:
            db_stats[rel] = handle_db(src_root, dst_root, rel, rw)
        except Exception as e:
            db_stats[rel] = {"error": str(e)}
            warn(f"Could not process DB {rel}: {e}")

    return {"files": rep, "dbs": db_stats}


# ---------------------------------------------------------------- verify

def verify(dst: Account, src: Optional[Account],
           rw: Optional[Rewriter]) -> bool:
    heading(f"VERIFICATION  target={dst.id} ({dst.label})")
    good = True

    # 1) leftover source tokens
    if rw and rw.active:
        needles = rw.leftover_needles()
        leftovers: Dict[str, List[str]] = {}
        for tree in INCLUDE_TREES:
            base = dst.path / tree
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(dst.path).as_posix()
                hits = []
                for n in needles:
                    # it may also remain in the file name
                    if n in rel:
                        hits.append(f"name:{n}")
                        continue
                    if is_db_related(rel):
                        # db contents are scanned separately below
                        continue
                    if f.stat().st_size > MAX_TEXT_BYTES:
                        continue
                    try:
                        data = f.read_bytes()
                    except OSError:
                        continue
                    if is_binary(data):
                        continue
                    txt = read_text(data)
                    if txt and n in txt:
                        hits.append(n)
                if hits:
                    leftovers[rel] = hits
        if leftovers:
            good = False
            err(f"account/agent identity remains ({len(leftovers)} files):")
            for k, v in list(leftovers.items())[:12]:
                err(f"    {k} -> {', '.join(sorted(set(v)))}")
        else:
            ok("no old identity left in text files")

        # DB content scan
        db_left: List[str] = []
        for rel in discover_dbs(dst.path):
            p = dst.path / rel
            if not p.is_file():
                continue
            try:
                con = _open_ro(p)
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                for t in tables:
                    if any(t.startswith(n) for n in needles):
                        db_left.append(f"{rel}#table:{t}")
                    try:
                        cur = con.execute(f'SELECT * FROM "{t}"')
                        cols = [d[0] for d in cur.description or []]
                        for row in cur.fetchall():
                            for c, v in zip(cols, row):
                                if isinstance(v, str):
                                    for n in needles:
                                        if n in v or n in c:
                                            db_left.append(
                                                f"{rel}#{t}.{c}:{n}")
                                            raise StopIteration
                    except StopIteration:
                        pass
                    except Exception:
                        pass
                con.close()
            except SystemExit:
                raise
            except Exception as e:
                warn(f"Could not scan DB {rel}: {e}")
        if db_left:
            good = False
            err("Old identity left in DB:")
            for x in sorted(set(db_left))[:12]:
                err(f"    {x}")
        else:
            ok("no old identity left in databases")

    # 2) DB health + counts
    for rel in discover_dbs(dst.path):
        p = dst.path / rel
        if not p.is_file():
            continue
        try:
            con = sqlite3.connect(p, timeout=5)
            chk = con.execute("PRAGMA quick_check").fetchone()[0]
            simple, _ = _classify_tables(con)
            counts = {}
            for t in simple:
                try:
                    counts[t] = con.execute(
                        f'SELECT count(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    pass
            con.close()
            if chk != "ok":
                good = False
                err(f"{rel}: quick_check = {chk}")
            else:
                ok(f"{rel}: healthy  {counts}")
        except Exception as e:
            good = False
            err(f"{rel}: {e}")

    # 3) chat count
    n = dst.conversation_count
    if n:
        ok(f"target conversation files: {n}")
    else:
        warn("no target conversation files")

    if src:
        sn = src.conversation_count
        if sn and n and n < sn:
            warn(f"source had {sn} chats, target has {n} files")
    return good


# ---------------------------------------------------------------- commands

def cmd_accounts(args: argparse.Namespace) -> int:
    home = accio_home(args.home)
    accounts = list_accounts(home)
    if not accounts:
        err(f"no accounts: {home / 'accounts'}")
        return 1
    meta_path = remembered_accounts_path()
    heading(f"ACCOUNTS  ({home / 'accounts'})")
    if meta_path:
        log(f"  meta: {meta_path}")
    else:
        warn("remembered-accounts.json not found (name/email will not be shown)")
    log()
    log(f"  {'#':<3} {'accountId':<14} {'name':<18} {'email':<26} "
        f"{'accioId':<16} {'chats':>6} {'agent':>5} {'size':>8}")
    log("  " + "-" * 104)
    for i, a in enumerate(accounts):
        log(f"  {i:<3} {a.id:<14} {(a.name or '-'):<18} "
            f"{(a.email or '-'):<26} {(a.accio_id or '?'):<16} "
            f"{a.conversation_count:>6} {len(a.agent_ids):>5} "
            f"{human(a.size_bytes):>8}")
    log()
    log("  tip: in commands you can use the accountId, the number from this list, "
        "or a name/email fragment.")
    if args.json:
        print(json.dumps([{
            "index": i, "id": a.id, "name": a.name, "email": a.email,
            "accio_id": a.accio_id, "conversations": a.conversation_count,
            "agents": a.agent_ids, "size": a.size_bytes,
        } for i, a in enumerate(accounts)], ensure_ascii=False, indent=2))
    return 0


def _pair(args: argparse.Namespace) -> Tuple[Account, Account, Rewriter]:
    home = accio_home(args.home)
    src = resolve_account(home, args.src)
    dst = resolve_account(home, args.dst)
    if src.id == dst.id:
        raise SystemExit("source and target cannot be the same account.")
    rw = build_rewriter(src, dst, args.suffix_len)
    if args.accio_id:
        rw.src_accio = args.accio_id
        rw = _rebuild(rw)
    if args.accio_id_to:
        rw.dst_accio = args.accio_id_to
        rw = _rebuild(rw)
    return src, dst, rw


def _rebuild(rw: Rewriter) -> Rewriter:
    """Rebuild to apply the accioId values passed via the CLI."""
    return Rewriter(src_acct=rw.src_acct, dst_acct=rw.dst_acct,
                    src_accio=rw.src_accio, dst_accio=rw.dst_accio,
                    suffix_len=rw.suffix_len, did_map=rw.did_map)


def cmd_plan(args: argparse.Namespace) -> int:
    src, dst, rw = _pair(args)
    heading(f"PLAN  {src.id} ({src.label})  ->  {dst.id} ({dst.label})")
    log()
    log("  identity transformation:")
    for line in rw.describe():
        log(f"    {line}")

    log()
    log("  surface to transfer:")
    rep = CopyReport()
    total = 0
    total_n = 0
    for tree in INCLUDE_TREES:
        base = src.path / tree
        if not base.exists():
            log(f"    {tree:<20} (none)")
            continue
        n = b = 0
        skip = 0
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(src.path).as_posix()
            if is_db_related(rel) or f.name in SKIP_FILE_NAMES \
                    or _skip_runtime(rel):
                skip += 1
                continue
            n += 1
            try:
                b += f.stat().st_size
            except OSError:
                pass
        total += b
        total_n += n
        log(f"    {tree:<20} {n:>5} files  {human(b):>9}"
            + (f"   ({skip} skipped)" if skip else ""))
    log(f"    {'TOTAL (files)':<20} {total_n:>5} files  {human(total):>9}")

    log()
    log("  databases:")
    for rel in discover_dbs(src.path):
        s = src.path / rel
        d = dst.path / rel
        mode = "MISSING->copy+convert" if not d.exists() else "EXISTS->merge rows"
        log(f"    {rel:<38} {human(s.stat().st_size):>9}   target: {mode}")

    log()
    log("  sample transformations:")
    samples: List[str] = []
    for tree in ("agents", "conversations", "tasks"):
        base = src.path / tree
        if base.exists():
            samples += [x.name for x in base.rglob("*")][:40]
    shown = 0
    for s in samples:
        if rw(s) != s:
            log(f"    {s}\n      -> {rw(s)}")
            shown += 1
            if shown >= 4:
                break
    if not shown:
        log("    (no examples found)")

    if dst.conversation_count:
        warn(f"target already has {dst.conversation_count} conversation files; "
             f"existing ones are kept, conflicting ones are not added.")
    log()
    log("  no writes were made. To apply:  apply "
        f"-f {src.id} -t {dst.id}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    home = accio_home(args.home)
    acc = resolve_account(home, args.account)
    heading(f"BACKUP  {acc.id} ({acc.label})")
    backup_account(home, acc)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    home = accio_home(args.home)
    src, dst, rw = _pair(args)

    heading(f"TRANSFER  {src.id} ({src.label})  ->  {dst.id} ({dst.label})")
    log("  identity transformation:")
    for line in rw.describe():
        log(f"    {line}")

    if not rw.active:
        warn("no transformation (source and target identities are the same) - operation cancelled")
        return 1

    if app_running() and not args.force:
        err("Accio is running. Close it or continue with --force (risk is yours).")
        return 1

    dbs = [dst.path / r for r in discover_dbs(dst.path)]
    dbs += [dst.path / "conversations" / "conv-index.db",
            dst.path / "msg-archive" / "archive.db"]
    assert_dbs_unlocked(dbs)

    if not args.yes and not args.no_backup:
        ans = input(f"  target {dst.id} will be backed up, continue? (y/N): ").strip().lower()
        if ans != "y":
            log("  cancelled")
            return 1

    if not args.no_backup:
        backup_account(home, dst)

    t0 = time.time()
    result = run_migration(src.path, dst.path, rw, overwrite=args.overwrite)
    rep: CopyReport = result["files"]  # type: ignore[assignment]
    dt = time.time() - t0

    heading("RESULT")
    log(f"    copied          : {rep.copied} files "
        f"({human(rep.bytes)})  [{dt:.1f}s]")
    log(f"    rewritten       : {rep.rewritten_files} file contents")
    log(f"    existing/skipped: {rep.existing}")
    log(f"    binary copies   : {rep.binary}")
    for tree, (c, b) in sorted(rep.per_tree.items()):
        log(f"      {tree:<20} {c:>5}  {human(b):>9}")
    log()
    log("    databases:")
    for rel, st in result["dbs"].items():  # type: ignore[union-attr]
        log(f"      {rel:<38} {st}")

    good = verify(dst, src, rw)
    if good:
        heading("DONE")
        log(f"  chats in {src.id} were transferred to account {dst.id}.")
        log("  Now open Accio and log in with the target account.")
        log("  To undo, use the backup: ~/.accio/backups/<time>_<accountId>")
        return 0
    heading("CAUTION")
    log("  some verifications failed; the backup is intact, "
        "review the output before opening Accio.")
    return 2


def cmd_export(args: argparse.Namespace) -> int:
    home = accio_home(args.home)
    acc = resolve_account(home, args.account)
    out = Path(args.out).expanduser()
    if out.is_dir():
        out = out / f"accio_{acc.id}_{datetime.now():%Y%m%d_%H%M%S}.accioexport"
    out.parent.mkdir(parents=True, exist_ok=True)

    heading(f"EXPORT  {acc.id} ({acc.label})")
    files: List[dict] = []
    seen_db: set = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for tree in INCLUDE_TREES:
            base = acc.path / tree
            if not base.exists():
                continue
            for f in sorted(base.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(acc.path).as_posix()
                if f.name in SKIP_FILE_NAMES or _skip_runtime(rel):
                    continue
                # Include DB side files (-wal): they are processed after the DB when read
                if not is_db_related(rel) and rel in seen_db:
                    continue
                z.write(f, rel)
                files.append({"path": rel, "size": f.stat().st_size,
                              "sha256": sha256_file(f)})
                if rel.endswith(".db"):
                    seen_db.add(rel)

        slen = detect_suffix_len(acc.path, acc.id, "")
        manifest = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "created": datetime.now().isoformat(timespec="seconds"),
            "host": os.environ.get("COMPUTERNAME")
                    or os.environ.get("HOSTNAME") or "",
            "source": {
                "account_id": acc.id,
                "accio_id": acc.accio_id or detect_accio_id(acc.path),
                "name": acc.name,
                "email": acc.email,
                "suffix_len": slen,
                "dids": acc.agent_ids,
                "conversations": acc.conversation_count,
            },
            "files": files,
            "dbs": [f["path"] for f in files if f["path"].endswith(".db")],
        }
        z.writestr("manifest.json", json.dumps(manifest, indent=2,
                                                ensure_ascii=False))

    log(f"  package: {out}")
    log(f"  files  : {len(files)}")
    log(f"  size   : {human(out.stat().st_size)}")
    log()
    log("  Transfer to another machine:")
    log(f"    accio_migrate.py import -i \"{out}\" -t <TARGET_ACCOUNT>")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    home = accio_home(args.home)
    pkg = Path(args.file).expanduser()
    if not pkg.is_file():
        raise SystemExit(f"package not found: {pkg}")

    tmp = Path(tempfile.mkdtemp(prefix="accio_imp_"))
    try:
        with zipfile.ZipFile(pkg) as z:
            names = z.namelist()
            if "manifest.json" not in names:
                raise SystemExit("no manifest.json -> this is not an accio package")
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != FORMAT_NAME:
                raise SystemExit(f"unknown format: {manifest.get('format')}")
            z.extractall(tmp)

        src_meta = manifest.get("source", {})
        src_id = str(src_meta.get("account_id") or "")
        dst = resolve_account(home, args.dst)
        if src_id and src_id == dst.id:
            raise SystemExit("the package source is the same account as the target.")

        slen = args.suffix_len or int(src_meta.get("suffix_len")
                                      or DEFAULT_SUFFIX_LEN)
        did_map: Dict[str, str] = {}
        src_dids = list(src_meta.get("dids") or [])
        dst_dids = dst.agent_ids
        if len(src_dids) == 1 and len(dst_dids) == 1:
            did_map[src_dids[0]] = dst_dids[0]

        src_accio = str(src_meta.get("accio_id") or "")
        dst_accio = args.accio_id_to or dst.accio_id or ""
        if src_accio and not dst_accio:
            warn("target accioId is unknown; accio tokens will not be converted")

        rw = Rewriter(src_acct=src_id, dst_acct=dst.id,
                      src_accio=src_accio, dst_accio=dst_accio,
                      suffix_len=slen, did_map=did_map)

        heading(f"IMPORT  {pkg.name}  ->  {dst.id} ({dst.label})")
        log(f"  package source: {src_id or '?'} "
            f"({src_meta.get('name') or '-'}, {src_meta.get('email') or '-'})")
        log(f"  created       : {manifest.get('created')}  "
            f"host={manifest.get('host') or '?'}")
        log("  identity transformation:")
        for line in rw.describe():
            log(f"    {line}")

        if app_running() and not args.force:
            err("Accio is running. Close it or continue with --force.")
            return 1
        dbs = [dst.path / r for r in discover_dbs(dst.path)]
        assert_dbs_unlocked(dbs)

        if not args.no_backup:
            if not args.yes:
                ans = input(f"  target {dst.id} will be backed up, continue? (y/N): ").strip().lower()
                if ans != "y":
                    log("  cancelled")
                    return 1
            backup_account(home, dst)

        t0 = time.time()
        result = run_migration(tmp, dst.path, rw, overwrite=args.overwrite)
        rep: CopyReport = result["files"]  # type: ignore[assignment]
        heading("RESULT")
        log(f"    copied          : {rep.copied} files "
            f"({human(rep.bytes)})  [{time.time() - t0:.1f}s]")
        log(f"    rewritten       : {rep.rewritten_files} file contents")
        log(f"    existing/skipped: {rep.existing}")
        log("    databases:")
        for rel, st in result["dbs"].items():  # type: ignore[union-attr]
            log(f"      {rel:<38} {st}")

        good = verify(dst, None, rw)
        if good:
            heading("DONE")
            log(f"  package applied to account {dst.id}. Open Accio.")
            return 0
        heading("CAUTION")
        log("  there are verification warnings; the backup is intact.")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_verify(args: argparse.Namespace) -> int:
    home = accio_home(args.home)
    dst = resolve_account(home, args.dst)
    src = resolve_account(home, args.src) if args.src else None
    rw = None
    if src:
        rw = build_rewriter(src, dst, args.suffix_len)
        if args.accio_id:
            rw.src_accio = args.accio_id
            rw = _rebuild(rw)
        if args.accio_id_to:
            rw.dst_accio = args.accio_id_to
            rw = _rebuild(rw)
    return 0 if verify(dst, src, rw) else 2


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="accio_migrate.py",
        description="Tool for transferring chats between Accio accounts "
                    "(back up / transfer / import-export / verify).")
    p.add_argument("--home", help="Accio data directory (default: ~/.accio)")
    sub = p.add_subparsers(dest="cmd")

    def common(sp, with_pair=False, with_acc=False, with_account=False):
        # SUPPRESS: do not FORCE the subcommand to write its own value; do not erase
        # the value given at the top level in the form `--home X <command>` (argparse
        # copies subparser defaults into the upper namespace).
        sp.add_argument("--home", default=argparse.SUPPRESS,
                        help="Accio data directory (default: ~/.accio)")
        if with_account:
            sp.add_argument("-a", "--account", required=True,
                            help="accountId / list number / name")
        if with_pair:
            sp.add_argument("-f", "--src", required=True,
                            help="source account")
            sp.add_argument("-t", "--dst", required=True,
                            help="target account")
            sp.add_argument("--suffix-len", type=int, default=0,
                            help="account suffix length (0=auto-detect)")
        if with_acc:
            sp.add_argument("--accio-id", default="",
                            help="source accioId (manual)"
                                 " (required if there is no metadata)")
            sp.add_argument("--accio-id-to", default="",
                            help="target accioId (manual)")

    sp = sub.add_parser("accounts", help="list accounts")
    sp.add_argument("--home", default=argparse.SUPPRESS)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser("plan", help="show what would be transferred (does not write)")
    common(sp, with_pair=True, with_acc=True)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("backup", help="full backup of the account")
    common(sp, with_account=True)
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("apply", help="transfer from source to target")
    common(sp, with_pair=True, with_acc=True)
    sp.add_argument("-y", "--yes", action="store_true",
                    help="do not ask for confirmation")
    sp.add_argument("--no-backup", action="store_true",
                    help="skip backup (NOT RECOMMENDED)")
    sp.add_argument("--overwrite", action="store_true",
                    help="overwrite files with the same name in the target")
    sp.add_argument("--force", action="store_true",
                    help="continue even while Accio is running (risk is yours)")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("export", help="produce a portable package")
    common(sp, with_account=True)
    sp.add_argument("-o", "--out", required=True,
                    help="output file (.accioexport)")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="apply the package to the target account")
    sp.add_argument("--home", default=argparse.SUPPRESS)
    sp.add_argument("-i", "--file", required=True, help="package file")
    sp.add_argument("-t", "--dst", required=True, help="target account")
    sp.add_argument("--suffix-len", type=int, default=0)
    sp.add_argument("--accio-id-to", default="")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.add_argument("--no-backup", action="store_true")
    sp.add_argument("--overwrite", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("verify", help="verify the target account")
    common(sp, with_pair=False, with_acc=True)
    sp.add_argument("-t", "--dst", required=True, help="target account")
    sp.add_argument("-f", "--src", default="", help="source account (optional)")
    sp.add_argument("--suffix-len", type=int, default=0,
                    help="account suffix length (0=auto-detect)")
    sp.set_defaults(func=cmd_verify)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    except SystemExit as e:
        if e.code in (None, 0):
            return 0
        err(str(e))
        return int(e.code) if isinstance(e.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
