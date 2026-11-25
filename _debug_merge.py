# -*- coding: utf-8 -*-
"""Show merge_into skip reasons (synthetic data)."""
import json, shutil, sqlite3, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accio_migrate as M

SRC_ACCT, DST_ACCT = "1000000001", "2000000002"
SRC_ACCIO, DST_ACCIO = "4500000000001", "4500000000002"
CID = "CID-79000001U1790094-E33EF0-1230-6DC8CC"
SRC_DID = "DID-AAAAAA-AAAAAAAAU1790091-0000-AAAAAA"
DST_DID = "DID-BBBBBB-BBBBBBBBU1790106-1111-BBBBBB"
rw = M.Rewriter(src_acct=SRC_ACCT, dst_acct=DST_ACCT, src_accio=SRC_ACCIO,
                dst_accio=DST_ACCIO, suffix_len=6, did_map={SRC_DID: DST_DID})

root = Path(tempfile.mkdtemp(prefix="dbg_"))
try:
    sdb = root / "src" / "msg-archive" / "archive.db"
    sdb.parent.mkdir(parents=True)
    con = sqlite3.connect(sdb)
    con.executescript(f"""
      CREATE TABLE archive_meta ( request_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL
        DEFAULT 0, final_seq INTEGER, byte_size INTEGER NOT NULL DEFAULT 0,
        released_at INTEGER, conv_id TEXT );
      CREATE TABLE "u_ai_acciowork_{SRC_ACCIO}_1790094123156" (
        seq INTEGER PRIMARY KEY, msg_seq TEXT NOT NULL, kind TEXT NOT NULL,
        body TEXT NOT NULL, is_final INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL );
    """)
    con.execute("INSERT INTO archive_meta VALUES (?,?,?,?,?,?,?,?,?)",
                (f"AI_AccioWork_{SRC_ACCIO}_1790094123156",
                 f"u_ai_acciowork_{SRC_ACCIO}_1790094123156",
                 1, 1, 3, 3, 100, None, None))
    for i in range(1, 4):
        con.execute(f'INSERT INTO "u_ai_acciowork_{SRC_ACCIO}_1790094123156" '
                    "VALUES (?,?,?,?,?,?)",
                    (i, f"AI_AccioWork_{SRC_ACCIO}_{1790094123156+i}",
                     "text", json.dumps({"cid": CID, "acct": SRC_ACCT}), 1, 1))
    con.commit()
    src = M._open_ro(sdb)

    print("PRAGMA table_info outputs (i0=name i4=dflt i5=pk):")
    for r in src.execute('PRAGMA main.table_info("archive_meta")'):
        print("   meta  :", r)
    for r in src.execute('PRAGMA main.table_info("u_ai_acciowork_'
                         f'{SRC_ACCIO}_1790094123156")'):
        print("   data  :", r)

    print("\nOur _table_info() transformation (last element should be pk):")
    for t in ("archive_meta", f"u_ai_acciowork_{SRC_ACCIO}_1790094123156"):
        print(f"   {t}:")
        for c in M._table_info(src, "main", t):
            print("      ", c)

    simple, fts = M._classify_tables(src)
    print("\nsrc_simple =", simple, " fts =", fts)

    ddl = {r[0]: r[1] for r in src.execute(
        "SELECT name, COALESCE(sql,'') FROM sqlite_master "
        "WHERE type='table'")}
    for tbl in simple:
        cols = M._table_info(src, "main", tbl)
        autopk = M._has_autopk(ddl.get(tbl, ""), cols)
        pks = [c[0] for c in cols if c[3]] or [c[0] for c in cols]
        print(f"   {tbl}: autopk={autopk}  key_cols={pks}  (c[3]={[c[3] for c in cols]})")
    src.close()
finally:
    shutil.rmtree(root, ignore_errors=True)
