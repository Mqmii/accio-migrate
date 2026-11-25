"""Read-only schema dump - writes nothing, opens with mode=ro.

Usage: python _inspect_schema.py <accountId|index|name>
The account is resolved from ACCIO_HOME (default ~/.accio).
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accio_migrate as M  # noqa: E402

if len(sys.argv) < 2:
    raise SystemExit("Usage: python _inspect_schema.py <account>")

acct = M.resolve_account(M.accio_home(None), sys.argv[1])
ACCT = acct.path
targets = [
    ACCT / "conversations" / "conv-index.db",
    ACCT / "msg-archive" / "archive.db",
    ACCT / "agents" / "sqlite" / "index.db",
]

for p in targets:
    print("=" * 70)
    print(p)
    if not p.is_file():
        print("  (none)")
        continue
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        for t, n, s in rows:
            print(f"  [{t}] {n}")
            if s:
                print("    " + " ".join(s.split()))
        print("  -- row counts --")
        tbls = [n for t, n, _ in rows if t == "table"]
        for tname in tbls:
            try:
                cnt = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
                print(f"    {tname}: {cnt}")
            except Exception as e:
                print(f"    {tname}: ERR {e}")
        con.close()
    except Exception as e:
        print("  ERR", e)
sys.exit(0)
