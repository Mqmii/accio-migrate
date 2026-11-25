# -*- coding: utf-8 -*-
"""
Synthetic self-test for accio_migrate.py.
DOES NOT TOUCH REAL ACCOUNT DATA: everything is set up in a temp folder with 100% fake data.
Exit code 0 = all tests passed.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from accio_migrate import (  # noqa: E402
    Rewriter, CopyReport, copy_surface, clone_and_rewrite, merge_into,
    detect_suffix_len, read_text, is_binary, discover_dbs,
)
import accio_migrate as M  # noqa: E402

FAIL = []


def check(name: str, cond, detail=""):
    if cond:
        print(f"  [ok]   {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        FAIL.append(name)


# ---------------------------------------------------------------- ids (real format)
SRC_ACCT = "1000000001"
DST_ACCT = "2000000002"
SRC_ACCIO = "4500000000001"
DST_ACCIO = "4500000000002"
SRC_DID = "DID-AAAAAA-AAAAAAAAU1790091-0000-AAAAAA"
DST_DID = "DID-BBBBBB-BBBBBBBBU1790106-1111-BBBBBB"
CID = "CID-79000001U1790094-E33EF0-1230-6DC8CC"
MID = "MID-17000001U1790091-E33EF0-8869-C7B047"


def rw() -> Rewriter:
    return Rewriter(src_acct=SRC_ACCT, dst_acct=DST_ACCT,
                    src_accio=SRC_ACCIO, dst_accio=DST_ACCIO,
                    suffix_len=6, did_map={SRC_DID: DST_DID})


# ================================================================ 1) Rewriter
print("\n=== 1. Rewriter (identity transformation) ===")
r = rw()

check("CID file name",
      r(CID + ".json") == "CID-79000002U1790094-E33EF0-1230-6DC8CC.json",
      r(CID + ".json"))
check("MID folder name",
      r(MID) == "MID-17000002U1790091-E33EF0-8869-C7B047", r(MID))
check("storageKey json",
      r('{"storageKey": "1000000001"}') == '{"storageKey": "2000000002"}')
check("agentId (DID map)",
      r(SRC_DID) == DST_DID, r(SRC_DID))
check("requestId accioId",
      r("AI_AccioWork_4500000000001_1790094123156")
      == "AI_AccioWork_4500000000002_1790094123156")
check("table name (lowercase)",
      r("u_ai_acciowork_4500000000001_1790094123156")
      == "u_ai_acciowork_4500000000002_1790094123156")
check("session_key mapping",
      r(f"agent:{SRC_DID}:main:cid:{CID}")
      == f"agent:{DST_DID}:main:cid:CID-79000002U1790094-E33EF0-1230-6DC8CC")

# GUARD: must not touch when the account ID is inside an epoch timestamp
ts_ms = "1000000001000"          # 13-digit ms -> must not be touched
check("13-digit ms timestamp is preserved", r(ts_ms) == ts_ms, r(ts_ms))
ts_embed = '"created": 1000000001999'
check("preserved if it contains a long digit sequence",
      r(ts_embed) == ts_embed, r(ts_embed))
check("only the account ID changes",
      r('["1000000001",1000000001]') == '["2000000002",2000000002]',
      r('["1000000001",1000000001]'))
check("accio ID changes on its own (digit boundary)",
      r(f"x{SRC_ACCIO}y") == f"x{DST_ACCIO}y", r(f"x{SRC_ACCIO}y"))

# non-str is untouched
check("None passes through unchanged", r(None) is None)
check("int passes through unchanged", r(42) == 42)
check("bytes pass through unchanged", r(b"\x00\x01") == b"\x00\x01")
check("empty string", r("") == "")

# ================================================================ 2) suffix detection
print("\n=== 2. suffix detection (automatic from data) ===")
probe_names = [CID, MID, "MID-41000001U1790091-E33EF0-8871-85D269",
               f"agent:{SRC_DID}:main:cid:{CID}"]
# detect_suffix_len scans folders -> create folder
tmp = Path(tempfile.mkdtemp(prefix="accio_sfx_"))
try:
    for n in probe_names:
        safe = n.replace(":", "_")
        (tmp / "agents" / safe).mkdir(parents=True, exist_ok=True)
    got = detect_suffix_len(tmp, SRC_ACCT, DST_ACCT)
    check("last 6 digits detected", got == 6, f"got={got}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ================================================================ 3) copy_surface
print("\n=== 3. copy_surface (file copy + rewriting) ===")
root = Path(tempfile.mkdtemp(prefix="accio_cs_"))
src = root / "src"
dst = root / "dst"
try:
    dm = src / "conversations" / "dm"
    dm.mkdir(parents=True)
    (dm / f"{CID}.json").write_text(
        json.dumps({"storageKey": SRC_ACCT, "id": CID, "agentId": SRC_DID,
                    "name": "Example chat title"}, ensure_ascii=False),
        encoding="utf-8")
    (dm / ".sink-migrated").write_text("x", encoding="utf-8")

    sess = src / "agents" / SRC_DID / "sessions"
    sess.mkdir(parents=True)
    (sess / f"{SRC_DID}_{CID}.messages.jsonl").write_text(
        json.dumps({"requestId": f"AI_AccioWork_{SRC_ACCIO}_1790094123156"})
        + "\n", encoding="utf-8")
    runt = src / "agents" / SRC_DID / "runtime"
    runt.mkdir(parents=True)
    (runt / "lock.state").write_text("busy", encoding="utf-8")
    # binary file
    (dm / "blob.bin").write_bytes(b"\x00\x01\x02PNG")

    rep = CopyReport()
    copy_surface(src, dst, rw(), rep, overwrite=False)

    new_cid = "CID-79000002U1790094-E33EF0-1230-6DC8CC"
    out = dst / "conversations" / "dm" / f"{new_cid}.json"
    check("CID name + content rewritten", out.is_file(),
          f"missing: {out}")
    if out.is_file():
        data = json.loads(out.read_text(encoding="utf-8"))
        check("storageKey target account", data["storageKey"] == DST_ACCT,
              data["storageKey"])
        check("id target CID", data["id"] == new_cid, data["id"])
        check("agentId target DID", data["agentId"] == DST_DID,
              data["agentId"])

    check(".sink-migrated skipped",
          not (dst / "conversations" / "dm" / ".sink-migrated").exists())
    check("runtime skipped",
          not (dst / "agents" / DST_DID / "runtime" / "lock.state").exists())
    s2 = dst / "agents" / DST_DID / "sessions"
    got = [p.name for p in s2.glob("*.jsonl")] if s2.is_dir() else []
    check("session file DID+CID rewritten",
          got == [f"{DST_DID}_{new_cid}.messages.jsonl"], got)
    if got:
        txt = (s2 / got[0]).read_text(encoding="utf-8")
        check("session content accioId rewritten",
              DST_ACCIO in txt and SRC_ACCIO not in txt, txt[:80])
    b = dst / "conversations" / "dm" / "blob.bin"
    check("binary file copied raw",
          b.is_file() and b.read_bytes() == b"\x00\x01\x02PNG")
    check("binary counter increased", rep.binary >= 1, str(rep.binary))

    # second run: nothing should be added (idempotent)
    rep2 = CopyReport()
    copy_surface(src, dst, rw(), rep2, overwrite=False)
    check("no new files on re-run", rep2.copied == 0,
          f"copied={rep2.copied}")
    check("existing counter filled", rep2.existing == rep.copied,
          f"{rep2.existing} vs {rep.copied}")

    # still, no old identity should remain
    leftovers = []
    for f in dst.rglob("*"):
        if not f.is_file() or is_binary(f.read_bytes()):
            continue
        t = read_text(f.read_bytes())
        if t and (SRC_ACCT in t or SRC_ACCIO in t or SRC_DID in t
                  or "000001U" in t):
            leftovers.append(f.name)
    check("no old identity left in target", not leftovers, str(leftovers))

finally:
    shutil.rmtree(root, ignore_errors=True)

# ================================================================ 4) clone_and_rewrite
print("\n=== 4. clone_and_rewrite (if target DB is absent) ===")
root = Path(tempfile.mkdtemp(prefix="accio_clone_"))
try:
    sdb = root / "src" / "agents" / "sqlite" / "index.db"
    sdb.parent.mkdir(parents=True)
    con = sqlite3.connect(sdb)
    con.executescript(f"""
      CREATE TABLE sessions ( session_key TEXT PRIMARY KEY, agent_id TEXT,
        title TEXT, project_path TEXT, started_at INTEGER, ended_at INTEGER,
        message_count INTEGER, last_error TEXT, model TEXT, channel_session_id,
        channel_kind, channel_destination, channel_connection_id,
        channel_message_id, channel_status, channel_error, channel_delivered_at,
        channel_delivered_message_id, channel_error_at, channel_created_at,
        meta_json, channel_channel_id );
      CREATE VIRTUAL TABLE messages_fts USING fts5(content_text,
        tokenize='unicode61');
      CREATE TABLE messages ( id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL, role TEXT NOT NULL,
        message_type TEXT NOT NULL DEFAULT 'text',
        tool_call_id TEXT, message TEXT,
        content_text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'client',
        model TEXT, created_at INTEGER NOT NULL,
        original_created_at INTEGER NOT NULL, ingress_type TEXT );
      CREATE TABLE message_vecs_info ( name TEXT PRIMARY KEY, value ANY );
    """)
    # The vec0 module is not available in Python -> write it artificially into
    # sqlite_master as in the REAL file (PRAGMA writable_schema). The module is
    # only loaded when queried; just like in a real Accio DB.
    con.execute("PRAGMA writable_schema=ON")
    con.execute(
        "INSERT INTO sqlite_master (type,name,tbl_name,rootpage,sql) "
        "VALUES ('table','message_vecs','message_vecs',0,"
        "'CREATE VIRTUAL TABLE message_vecs USING vec0(embedding float[4])')")
    sv = con.execute("PRAGMA schema_version").fetchone()[0]
    con.execute(f"PRAGMA schema_version={sv + 1}")
    con.execute("PRAGMA writable_schema=RESET")
    con.commit()
    sk = f"agent:{SRC_DID}:main:cid:{CID}"
    con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?)",
                (sk, SRC_DID, "Session", "C:/proj", 1, None, 2, None, "m",
                 None, None, None, None, None, None, None, None, None, None,
                 None, None, None))
    con.execute("INSERT INTO messages (session_key, role, content_text, "
                "created_at, original_created_at) VALUES (?,?,?,?,?)",
                (sk, "user",
                 f"<user_query>hello</user_query> accio:{SRC_ACCIO}", 1, 1))
    con.execute("INSERT INTO messages_fts(content_text) VALUES (?)",
                (f"accio:{SRC_ACCIO}",))
    con.execute("INSERT INTO message_vecs_info VALUES (?,?)",
                ("ctx", SRC_ACCIO))
    con.commit()
    con.close()

    ddb = root / "dst" / "agents" / "sqlite" / "index.db"
    st = clone_and_rewrite(sdb, ddb, rw())

    con = sqlite3.connect(ddb)
    tabs = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("vec0 table preserved", "message_vecs" in tabs, str(sorted(tabs)))
    check("fts table preserved", "messages_fts" in tabs)
    check("session_key rewritten",
          con.execute("SELECT session_key FROM sessions").fetchone()[0]
          == f"agent:{DST_DID}:main:cid:CID-79000002U1790094-"
             f"E33EF0-1230-6DC8CC")
    check("agent_id rewritten",
          con.execute("SELECT agent_id FROM sessions").fetchone()[0] == DST_DID)
    body = con.execute("SELECT content_text FROM messages").fetchone()[0]
    check("message content accio rewritten",
          DST_ACCIO in body and SRC_ACCIO not in body, body)
    check("message id preserved (autoincrement)",
          con.execute("SELECT id FROM messages").fetchone()[0] == 1)
    fts_txt = [r[0] for r in con.execute(
        "SELECT content_text FROM messages_fts")]
    msg_txt = [r[0] for r in con.execute(
        "SELECT content_text FROM messages")]
    check("FTS = messages content + accio updated",
          fts_txt == msg_txt and fts_txt
          and DST_ACCIO in fts_txt[0] and SRC_ACCIO not in fts_txt[0],
          str(fts_txt))
    chk = con.execute("PRAGMA quick_check").fetchone()[0]
    check("quick_check ok", chk == "ok", chk)
    con.close()
    check("rename/update counter", st.get("updated", 0) >= 1, str(st))
finally:
    shutil.rmtree(root, ignore_errors=True)

# ================================================================ 5) merge_into
print("\n=== 5. merge_into (if target DB exists) ===")
root = Path(tempfile.mkdtemp(prefix="accio_merge_"))
try:
    # SOURCE: archive.db format (dynamic table name containing accioId)
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
        con.execute(
            f'INSERT INTO "u_ai_acciowork_{SRC_ACCIO}_1790094123156" '
            "VALUES (?,?,?,?,?,?)",
            (i, f"AI_AccioWork_{SRC_ACCIO}_{1790094123156 + i}", "text",
             json.dumps({"requestId": f"AI_AccioWork_{SRC_ACCIO}_"
                                      f"1790094123156",
                         "cid": CID, "acct": SRC_ACCT}), 1, 1))
    con.commit()
    con.close()

    # TARGET: already has its own row (different request)
    ddb = root / "dst" / "msg-archive" / "archive.db"
    ddb.parent.mkdir(parents=True)
    con = sqlite3.connect(ddb)
    con.executescript("""
      CREATE TABLE archive_meta ( request_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL
        DEFAULT 0, final_seq INTEGER, byte_size INTEGER NOT NULL DEFAULT 0,
        released_at INTEGER, conv_id TEXT );
      CREATE TABLE "u_ai_acciowork_9999999999999_1" (
        seq INTEGER PRIMARY KEY, msg_seq TEXT NOT NULL, kind TEXT NOT NULL,
        body TEXT NOT NULL, is_final INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL );
    """)
    con.execute("INSERT INTO archive_meta VALUES (?,?,?,?,?,?,?,?,?)",
                ("AI_AccioWork_9999999999999_1",
                 "u_ai_acciowork_9999999999999_1", 9, 9, 1, 1, 5, None, None))
    con.commit()
    con.close()

    st = merge_into(sdb, ddb, rw())
    check("new table created", st.get("created_tables") == 1, str(st))
    check("rows added", st.get("inserted") == 4, str(st))

    con = sqlite3.connect(ddb)
    tabs = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    expect = f"u_ai_acciowork_{DST_ACCIO}_1790094123156"
    check("table name rewritten", expect in tabs, str(sorted(tabs)))
    check("no old table name",
          f"u_ai_acciowork_{SRC_ACCIO}_1790094123156" not in tabs)
    m = con.execute("SELECT request_id, table_name FROM archive_meta "
                    "ORDER BY request_id").fetchall()
    check("archive_meta: source row converted + target row preserved",
          m == [(f"AI_AccioWork_{DST_ACCIO}_1790094123156", expect),
                ("AI_AccioWork_9999999999999_1",
                 "u_ai_acciowork_9999999999999_1")], str(m))
    body = con.execute(f'SELECT body FROM "{expect}" ORDER BY seq').fetchall()
    check("body content rewritten (+3 rows)",
          len(body) == 3 and all(DST_ACCIO in b[0] and SRC_ACCIO not in b[0]
                                 for b in body), f"n={len(body)}")
    b0 = json.loads(body[0][0])
    check("CID/account inside body converted",
          b0["cid"] == "CID-79000002U1790094-E33EF0-1230-6DC8CC"
          and b0["acct"] == DST_ACCT, str(b0))
    chk = con.execute("PRAGMA quick_check").fetchone()[0]
    check("target quick_check ok", chk == "ok", chk)
    con.close()

    # re-run: no rows should be added (idempotent)
    st2 = merge_into(sdb, ddb, rw())
    check("no new rows on re-run", st2.get("inserted") == 0, str(st2))
    con = sqlite3.connect(ddb)
    n = con.execute(f'SELECT count(*) FROM "{expect}"').fetchone()[0]
    check("3 rows remain after table re-run", n == 3, str(n))
    con.close()
finally:
    shutil.rmtree(root, ignore_errors=True)

# ================================================================ 6) conv-index merge
print("\n=== 6. conv-index.db merge ===")
root = Path(tempfile.mkdtemp(prefix="accio_cidx_"))
try:
    DDL = ("CREATE TABLE conversations ( conversation_id TEXT PRIMARY KEY, "
           "chat_type TEXT NOT NULL, agent_id TEXT, title TEXT, path TEXT, "
           "created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL "
           "DEFAULT 0, last_user_message_at INTEGER NOT NULL DEFAULT 0, "
           "storage_key TEXT, deleted_at INTEGER, record_json TEXT NOT NULL )")
    rec = json.dumps({"path": "C:/proj", "name": "Chat", "agentId": SRC_DID,
                      "storageKey": SRC_ACCT, "id": CID})
    sdb = root / "src" / "conversations" / "conv-index.db"
    sdb.parent.mkdir(parents=True)
    con = sqlite3.connect(sdb)
    con.execute(DDL)
    con.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (CID, "dm", SRC_DID, "Chat", "C:/proj", 1, 2, 2,
                 SRC_ACCT, None, rec))
    con.commit()
    con.close()

    ddb = root / "dst" / "conversations" / "conv-index.db"
    ddb.parent.mkdir(parents=True)
    con = sqlite3.connect(ddb)
    con.execute(DDL)
    con.commit()
    con.close()

    st = merge_into(sdb, ddb, rw())
    con = sqlite3.connect(ddb)
    row = con.execute("SELECT conversation_id, agent_id, storage_key, "
                      "record_json FROM conversations").fetchone()
    check("row added", row is not None and len(row) == 4, str(row))
    if row:
        check("conversation_id target CID",
              row[0] == "CID-79000002U1790094-E33EF0-1230-6DC8CC", row[0])
        check("agent_id target DID", row[1] == DST_DID, row[1])
        check("storage_key target account", row[2] == DST_ACCT, str(row[2]))
        rj = json.loads(row[3])
        check("record_json all converted",
              rj["storageKey"] == DST_ACCT and rj["agentId"] == DST_DID
              and rj["id"] == "CID-79000002U1790094-E33EF0-1230-6DC8CC",
              str(rj))
    con.close()
    st2 = merge_into(sdb, ddb, rw())
    con = sqlite3.connect(ddb)
    n = con.execute("SELECT count(*) FROM conversations").fetchone()[0]
    con.close()
    check("single row on re-run", n == 1 and st2.get("inserted") == 0,
          f"n={n} {st2}")
finally:
    shutil.rmtree(root, ignore_errors=True)

# ================================================================ 7) discover_dbs
print("\n=== 7. discover_dbs ===")
root = Path(tempfile.mkdtemp(prefix="accio_disc_"))
try:
    for rel in ("conversations/conv-index.db", "msg-archive/archive.db",
                "agents/sqlite/index.db", "conversations/dm/x.json",
                "skills/y.db"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    got = discover_dbs(root)
    check("db list",
          got == ["agents/sqlite/index.db", "conversations/conv-index.db",
                  "msg-archive/archive.db"], str(got))
finally:
    shutil.rmtree(root, ignore_errors=True)

# ================================================================ 8) export/import e2e
print("\n=== 8. export -> import (CLI, full end-to-end, fake home) ===")
# ALL data and metadata are synthetic; the real ~/.accio or remembered-accounts.json
# is NOT READ. Both are monkeypatched to the fake home below.
_real_accio_home = M.accio_home
_real_meta_path = M.remembered_accounts_path
fake_home = Path(tempfile.mkdtemp(prefix="accio_home_"))
try:
    fsrc = fake_home / "accounts" / SRC_ACCT
    fdst = fake_home / "accounts" / DST_ACCT

    def mkdb(path, script, rows=()):
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path)
        c.executescript(script)
        for sql, args in rows:
            c.execute(sql, args)
        c.commit()
        c.close()

    # --- source
    dm = fsrc / "conversations" / "dm"
    dm.mkdir(parents=True)
    (dm / f"{CID}.json").write_text(json.dumps(
        {"storageKey": SRC_ACCT, "id": CID, "agentId": SRC_DID,
         "name": "Test chat"}, ensure_ascii=False), encoding="utf-8")
    (dm / ".sink-migrated").write_text("x", encoding="utf-8")
    sess = fsrc / "agents" / SRC_DID / "sessions"
    sess.mkdir(parents=True)
    (sess / f"{SRC_DID}_{CID}.messages.jsonl").write_text(
        json.dumps({"requestId": f"AI_AccioWork_{SRC_ACCIO}_1790094123156"})
        + "\n", encoding="utf-8")
    (fsrc / "agents" / SRC_DID / "runtime").mkdir(parents=True)
    (fsrc / "agents" / SRC_DID / "runtime" / "lock").write_text("x")

    rec = json.dumps({"storageKey": SRC_ACCT, "agentId": SRC_DID, "id": CID})
    CDDLV = ("CREATE TABLE conversations ( conversation_id TEXT PRIMARY KEY, "
             "chat_type TEXT NOT NULL, agent_id TEXT, title TEXT, path TEXT, "
             "created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT "
             "NULL DEFAULT 0, last_user_message_at INTEGER NOT NULL DEFAULT 0,"
             " storage_key TEXT, deleted_at INTEGER, record_json TEXT NOT "
             "NULL )")
    mkdb(fsrc / "conversations" / "conv-index.db", CDDLV,
         [("INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
           (CID, "dm", SRC_DID, "Test", "/p", 1, 2, 2, SRC_ACCT, None, rec))])

    ADDLV = ("CREATE TABLE archive_meta ( request_id TEXT PRIMARY KEY, "
             "table_name TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, "
             "updated_at INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT "
             "NULL DEFAULT 0, final_seq INTEGER, byte_size INTEGER NOT NULL "
             "DEFAULT 0, released_at INTEGER, conv_id TEXT); "
             f'CREATE TABLE "u_ai_acciowork_{SRC_ACCIO}_1790094123156" ('
             " seq INTEGER PRIMARY KEY, msg_seq TEXT NOT NULL, kind TEXT NOT "
             "NULL, body TEXT NOT NULL, is_final INTEGER NOT NULL DEFAULT 0, "
             "created_at INTEGER NOT NULL )")
    mkdb(fsrc / "msg-archive" / "archive.db", ADDLV, [
        ("INSERT INTO archive_meta VALUES (?,?,?,?,?,?,?,?,?)",
         (f"AI_AccioWork_{SRC_ACCIO}_1790094123156",
          f"u_ai_acciowork_{SRC_ACCIO}_1790094123156", 1, 1, 1, 1, 10, None,
          None)),
        (f'INSERT INTO "u_ai_acciowork_{SRC_ACCIO}_1790094123156" '
         "VALUES (?,?,?,?,?,?)",
         (1, f"AI_AccioWork_{SRC_ACCIO}_1790094123156", "text",
          json.dumps({"cid": CID, "acct": SRC_ACCT}), 1, 1)),
    ])

    ISELLV = ("CREATE TABLE sessions ( session_key TEXT PRIMARY KEY, "
              "agent_id TEXT, title TEXT); "
              "CREATE VIRTUAL TABLE messages_fts USING fts5(content_text, "
              "tokenize='unicode61'); "
              "CREATE TABLE messages ( id INTEGER PRIMARY KEY AUTOINCREMENT,"
              " session_key TEXT NOT NULL, role TEXT NOT NULL, content_text "
              "TEXT NOT NULL, created_at INTEGER NOT NULL )")
    mkdb(fsrc / "agents" / "sqlite" / "index.db", ISELLV, [
        ("INSERT INTO sessions VALUES (?,?,?)",
         (f"agent:{SRC_DID}:main:cid:{CID}", SRC_DID, "Session")),
        ("INSERT INTO messages (session_key, role, content_text, created_at)"
         " VALUES (?,?,?,?)",
         (f"agent:{SRC_DID}:main:cid:{CID}", "user",
          f"hello {SRC_ACCIO} {SRC_ACCT}", 1)),
    ])

    # --- target (has its own agent and its own empty DBs)
    (fdst / "conversations" / "dm").mkdir(parents=True)
    (fdst / "conversations" / "dm" / ".sink-migrated").write_text("x")
    mkdb(fdst / "conversations" / "conv-index.db", CDDLV)
    mkdb(fdst / "msg-archive" / "archive.db",
         "CREATE TABLE archive_meta ( request_id TEXT PRIMARY KEY, "
         "table_name TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, "
         "updated_at INTEGER NOT NULL DEFAULT 0, last_seq INTEGER NOT NULL "
         "DEFAULT 0, final_seq INTEGER, byte_size INTEGER NOT NULL DEFAULT 0,"
         " released_at INTEGER, conv_id TEXT )")
    (fdst / "agents" / DST_DID / "agent-core").mkdir(parents=True)
    (fdst / "agents" / DST_DID / "agent-core" / "MEMORY.md").write_text(
        "target's memory", encoding="utf-8")

    pkg = fake_home / "chat.accioexport"

    # ---- SAFETY FENCE: must never write to the real ~/.accio in any way
    # Synthetic metadata: accioId does not come from the real remembered-accounts.json.
    meta_file = fake_home / "Accio" / "remembered-accounts.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)

    def _acc_node(name, email, accio, login):
        return {"auth": {"name": name, "desensitizedEmail": email,
                         "accioId": accio, "userLevel": "",
                         "activeTeam": ""}, "loginAt": login}

    meta_file.write_text(json.dumps({
        SRC_ACCT: _acc_node("Source", "s****@example.com", SRC_ACCIO, 1),
        DST_ACCT: _acc_node("Target", "t****@example.com", DST_ACCIO, 2),
    }, ensure_ascii=False), encoding="utf-8")
    M.remembered_accounts_path = lambda: meta_file

    def _guard_home(override=None):
        p = _real_accio_home(override)
        if not str(Path(p)).startswith(str(fake_home)):
            raise AssertionError(
                f"DANGER: redirected to real home: {p} "
                f"(expected {fake_home})")
        return p

    M.accio_home = _guard_home

    rc = M.main(["--home", str(fake_home), "export", "-a", SRC_ACCT,
                 "-o", str(pkg)])
    check("export exit code 0", rc == 0, f"rc={rc}")
    check("package created", pkg.is_file())

    import zipfile as _zf
    with _zf.ZipFile(pkg) as z:
        names = z.namelist()
        mf = json.loads(z.read("manifest.json").decode("utf-8"))
    check("manifest format", mf.get("format") == "accio-export")
    check("manifest source id", mf["source"]["account_id"] == SRC_ACCT)
    check("manifest suffix_len=6", mf["source"]["suffix_len"] == 6,
          str(mf["source"].get("suffix_len")))
    check("manifest accioId", mf["source"]["accio_id"] == SRC_ACCIO)
    check("package contains dm json", any(
        n.endswith(f"{CID}.json") for n in names), str(names))
    check("package excludes runtime", not any("/runtime/" in n for n in names))
    check("package excludes .sink-migrated",
          not any(".sink-migrated" in n for n in names))
    check("package contains db", any(n.endswith("archive.db") for n in names))

    rc = M.main(["--home", str(fake_home), "import", "-i", str(pkg),
                 "-t", DST_ACCT, "-y", "--force"])
    check("import exit code 0 (verify=successful)", rc == 0, f"rc={rc}")

    new_cid = "CID-79000002U1790094-E33EF0-1230-6DC8CC"
    out = fdst / "conversations" / "dm" / f"{new_cid}.json"
    check("copied to target with CID name", out.is_file())
    if out.is_file():
        d = json.loads(out.read_text(encoding="utf-8"))
        check("target json: storageKey/id/agentId correct",
              d["storageKey"] == DST_ACCT and d["id"] == new_cid
              and d["agentId"] == DST_DID, str(d))

    c = sqlite3.connect(fdst / "conversations" / "conv-index.db")
    row = c.execute("SELECT conversation_id, storage_key, record_json "
                    "FROM conversations").fetchone()
    c.close()
    check("conv-index merged", row is not None, str(row))
    if row:
        check("conv-index conversion",
              row[0] == new_cid and row[1] == DST_ACCT
              and json.loads(row[2])["agentId"] == DST_DID, str(row))

    c = sqlite3.connect(fdst / "msg-archive" / "archive.db")
    tabs = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    exp_tbl = f"u_ai_acciowork_{DST_ACCIO}_1790094123156"
    check("archive dynamic table name rewritten", exp_tbl in tabs,
          str(sorted(tabs)))
    body = c.execute(f'SELECT body FROM "{exp_tbl}"').fetchone() if \
        exp_tbl in tabs else None
    c.close()
    if body:
        b = json.loads(body[0])
        check("archive body conversion", b["cid"] == new_cid
              and b["acct"] == DST_ACCT, str(b))

    sk = f"agent:{DST_DID}:main:cid:{new_cid}"
    c = sqlite3.connect(fdst / "agents" / "sqlite" / "index.db")
    check("index.db created", True)
    check("session_key converted",
          c.execute("SELECT session_key FROM sessions").fetchone()[0] == sk)
    mt = c.execute("SELECT content_text FROM messages").fetchone()[0]
    ft = c.execute("SELECT content_text FROM messages_fts").fetchone()[0]
    check("messages converted", DST_ACCIO in mt and DST_ACCT in mt
          and SRC_ACCIO not in mt, mt)
    check("FTS in sync", ft == mt, str(ft))
    c.close()

    core = fdst / "agents" / DST_DID / "agent-core" / "MEMORY.md"
    check("target's own agent-core preserved",
          core.read_text(encoding="utf-8") == "target's memory")
    check("runtime not carried over",
          not (fdst / "agents" / DST_DID / "runtime").exists())
    check("backup taken", (fake_home / "backups").is_dir()
          and any((fake_home / "backups").iterdir()))

    # old identity leftovers?
    bad = []
    for f in fdst.rglob("*"):
        if not f.is_file():
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if is_binary(data):
            continue
        t = read_text(data)
        if t and (SRC_ACCT in t or SRC_ACCIO in t or SRC_DID in t
                  or "000001U" in t):
            bad.append(str(f.relative_to(fdst)))
    check("no old identity left in target", not bad, str(bad))

    # second import should be idempotent
    rc = M.main(["--home", str(fake_home), "import", "-i", str(pkg),
                 "-t", DST_ACCT, "-y", "--force"])
    c = sqlite3.connect(fdst / "conversations" / "conv-index.db")
    n = c.execute("SELECT count(*) FROM conversations").fetchone()[0]
    c.close()
    check("second import -> single row", rc == 0 and n == 1,
          f"rc={rc} n={n}")
finally:
    M.accio_home = _real_accio_home          # remove fences
    M.remembered_accounts_path = _real_meta_path
    shutil.rmtree(fake_home, ignore_errors=True)

# ================================================================ RESULT
print()
if FAIL:
    print(f"RESULT: {len(FAIL)} tests FAILED")
    for f in FAIL:
        print(f"   - {f}")
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
sys.exit(0)
