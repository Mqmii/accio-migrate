"""Read-only sample data dump - writes nothing, opens with mode=ro.

Usage: python _inspect_data.py <SOURCE> <TARGET>
Accounts are resolved from ACCIO_HOME (default ~/.accio).
"""
import sqlite3, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accio_migrate as M  # noqa: E402

if len(sys.argv) < 3:
    raise SystemExit("Usage: python _inspect_data.py <SOURCE> <TARGET>")

home = M.accio_home(None)
src = M.resolve_account(home, sys.argv[1])
dst = M.resolve_account(home, sys.argv[2])
SRC, DST = src.path, dst.path
IDS = {"src_acct": src.id, "dst_acct": dst.id,
       "src_accio": src.accio_id, "dst_accio": dst.accio_id}


def ro(p):
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def tables(c):
    return [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]


def show(label, rows, limit=4):
    print(f"    {label}:")
    for r in rows[:limit]:
        print(f"      {r}")


for name, root in (("SOURCE", SRC), ("TARGET", DST)):
    print("=" * 78)
    print(f"{name}  {root}")

    # 1) conversations / conv-index.db
    p = root / "conversations" / "conv-index.db"
    print(f"  [--] {p.name}")
    try:
        c = ro(p)
        show("tables", tables(c))
        for row in c.execute(
            "SELECT conversation_id, storage_key, chat_type, agent_id, "
            "substr(record_json,1,300) FROM conversations LIMIT 4"):
            print(f"      cid={row[0]}\n      storage_key={row[1]} "
                  f"chat={row[2]}\n      agent={row[3]}\n      json={row[4]}\n")
        c.close()
    except Exception as e:
        print("      ERR", e)

    # 2) msg-archive / archive.db
    p = root / "msg-archive" / "archive.db"
    print(f"  [--] {p.name}")
    try:
        c = ro(p)
        show("tables", tables(c))
        for row in c.execute(
            "SELECT request_id, table_name, conv_id FROM archive_meta LIMIT 4"):
            print(f"      request_id={row[0]}\n      table_name={row[1]}\n"
                  f"      conv_id={row[2]}\n")
        c.close()
    except Exception as e:
        print("      ERR", e)

    # 3) agents/sqlite/index.db
    p = root / "agents" / "sqlite" / "index.db"
    print(f"  [--] {p.name}")
    try:
        c = ro(p)
        for row in c.execute(
            "SELECT session_key, agent_id, title, message_count "
            "FROM sessions LIMIT 4"):
            print(f"      session_key={row[0]}\n      agent={row[1]} "
                  f"title={row[2]!r} cnt={row[3]}\n")
        for row in c.execute(
            "SELECT id, session_key, role, substr(content_text,1,160) "
            "FROM messages LIMIT 3"):
            print(f"      msg#{row[0]} {row[2]} sess={row[1]}\n        "
                  f"{row[3]}\n")
        c.close()
    except Exception as e:
        print("      ERR", e)

# 4) Identity pattern scan: which ID appears in which format
print("=" * 78)
print("[--] PATTERN SCAN (on source)")
needles = list(IDS.values())
hits = {n: {} for n in needles}
scan_dirs = [SRC / "conversations", SRC / "agents" / "sqlite",
             SRC / "msg-archive", SRC / "tasks"]
exts = {".json", ".jsonl", ".jsonc", ".md", ".db", ".leaf", ".txt"}
scanned = 0
for d in scan_dirs:
    if not d.exists():
        continue
    for f in d.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in exts:
            continue
        try:
            data = f.read_bytes()
        except Exception:
            continue
        scanned += 1
        for n in needles:
            b = n.encode()
            cnt = data.count(b)
            if cnt:
                hits[n][str(f.relative_to(SRC))] = cnt
print(f"    files scanned: {scanned}")
for n, d in hits.items():
    print(f"    {n}: {'NONE' if not d else ''}")
    for k, v in list(d.items())[:8]:
        print(f"        {k}  x{v}")

# 5) compare AI_Accio prefixes
print("=" * 78)
print("[--] AI_Accio* PREFIX EXAMPLES")
pat = re.compile(rb"AI_Accio[A-Za-z]*_")
for name, root in (("source", SRC), ("target", DST)):
    found = set()
    for d in [root / "conversations", root / "agents", root / "msg-archive"]:
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            try:
                b = f.read_bytes()
            except Exception:
                continue
            for m in pat.findall(b):
                found.add(m.decode(errors="replace"))
    print(f"    {name}: {sorted(found)}")

# 6) CID / MID / DID examples
print("=" * 78)
print("[--] ID EXAMPLES (target)")
for d in [DST / "agents"]:
    for f in sorted(d.iterdir()):
        if f.is_dir():
            print(f"    dir {f.name}")
for f in sorted((DST / "conversations" / "dm").glob("*")):
    print(f"    dm {f.name}")
for f in sorted((DST / "conversations" / "team").glob("*")):
    print(f"    team {f.name}")
sys.exit(0)
