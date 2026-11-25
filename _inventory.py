# -*- coding: utf-8 -*-
"""Read-only inventory: which chat is in which account, and what was carried over."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accio_migrate as M  # noqa: E402


def ro(p: Path):
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def one(p: Path, sql: str, default=None):
    if not p.is_file():
        return default
    try:
        c = ro(p)
        v = c.execute(sql).fetchall()
        c.close()
        return v
    except Exception as e:
        return f"<err {e}>"


home = M.accio_home(None)
accounts = {a.id: a for a in M.list_accounts(home)}

# If no arguments are given, all accounts; otherwise the selected ones (id / index / name).
specs = sys.argv[1:] or sorted(accounts)
for spec in specs:
    try:
        a = M.resolve_account(home, spec)
    except SystemExit as e:
        print(f"{spec}: {e}")
        continue
    print("=" * 74)
    print(f"TARGET/SOURCE  {a.id}   {a.label}")
    print(f"  accioId       : {a.accio_id}")
    print(f"  chats         : {a.conversation_count}")
    print(f"  surface size  : {M.human(a.size_bytes)}")
    print(f"  agent DID     : {a.agent_ids}")

    for sub in ("dm", "team"):
        d = a.path / "conversations" / sub
        if not d.is_dir():
            continue
        cids = sorted({f.name.split(".")[0] for f in d.iterdir()
                       if f.is_file() and not f.name.startswith(".")})
        print(f"  conversations/{sub}/ -> {len(cids)} chats")
        for cid in cids:
            fs = [p for p in d.glob(cid + ".*") if p.is_file()]
            title = ""
            meta = d / (cid + ".json")
            if meta.is_file():
                try:
                    title = json.loads(
                        meta.read_text(encoding="utf-8")).get("name", "")
                except Exception:
                    title = "(unreadable)"
            tot = sum(p.stat().st_size for p in fs)
            print(f"    * title  : {title}")
            print(f"      CID    : {cid}")
            print(f"      files  : {len(fs)} items, {M.human(tot)}")

    rows = one(a.path / "conversations" / "conv-index.db",
               "SELECT conversation_id, title, chat_type FROM conversations")
    print(f"  conv-index.db -> {len(rows) if isinstance(rows, list) else rows} rows")
    if isinstance(rows, list):
        for r in rows:
            print(f"      {r[0]}  [{r[2]}] {r[1]}")

    rows = one(a.path / "msg-archive" / "archive.db",
               "SELECT name FROM sqlite_master WHERE type='table'")
    if isinstance(rows, list):
        print(f"  archive.db tables -> {[r[0] for r in rows]}")
        for (t,) in rows:
            if t == "archive_meta":
                n = one(a.path / "msg-archive" / "archive.db",
                        "SELECT count(*) FROM archive_meta")
                print(f"      archive_meta = {n}")
            else:
                n = one(a.path / "msg-archive" / "archive.db",
                        f'SELECT count(*) FROM "{t}"')
                print(f"      {t} = {n} rows (archived messages)")

    ib = a.path / "agents" / "sqlite" / "index.db"
    if ib.is_file():
        for t in ("sessions", "messages"):
            n = one(ib, f"SELECT count(*) FROM {t}")
            print(f"  index.db {t} -> {n}")
        n = one(ib, "SELECT role, count(*) FROM messages GROUP BY role")
        print(f"  index.db role distribution -> {n}")
    else:
        print("  index.db -> NONE")

    # what was NOT carried over (top-level folders intentionally excluded)
    if a.path.is_dir():
        alltop = sorted(p.name for p in a.path.iterdir() if p.is_dir())
        excluded = [n for n in alltop if n not in M.INCLUDE_TREES]
        print(f"  folders outside chat surface (NOT CARRIED) -> {excluded}")

print("=" * 74)
print("BACKUPS")
b = home / "backups"
if b.is_dir():
    for d in sorted(b.iterdir()):
        meta = d / ".meta.json"
        info = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
        size = M.human(sum(f.stat().st_size for f in d.rglob("*")
                           if f.is_file()))
        print(f"  {d.name}  ({size})  {info.get('date', '')}")
else:
    print("  none")
