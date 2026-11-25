#!/usr/bin/env python3
"""agent-archive ingester. Runs on the HOST only. Python 3.11+, stdlib only.

  ingest.py run        incremental ingest of every configured source
  ingest.py rebuild    re-derive normalized tables from the raw table
  ingest.py snapshot   write a raw-free, redacted analysis DB to data/snapshots/
  ingest.py stats      archive summary + entry types adapters don't understand

Trust model: every file under a source root was written by an untrusted agent.
The ingester never follows symlinks, only reads regular files, caps line and
file sizes, and only ever INSERTs into the archive -- nothing a container does
to its own transcripts can remove what has already been ingested.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import REGISTRY, Event, KeyInfo, norm_tool  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HEAD_BYTES = 4096

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw(
  id          TEXT PRIMARY KEY,   -- sha256(source, session_key, line)
  source      TEXT NOT NULL,
  harness     TEXT NOT NULL,
  session_key TEXT NOT NULL,
  line_no     INTEGER NOT NULL,
  line        TEXT NOT NULL,      -- verbatim, unredacted; never leaves the host
  ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_by_session ON raw(source, session_key, line_no);

CREATE TABLE IF NOT EXISTS file_state(
  source TEXT, rel_path TEXT, dev INTEGER, ino INTEGER, size INTEGER, mtime_ns INTEGER,
  offset INTEGER, line_count INTEGER, head_len INTEGER, head_hash TEXT,
  PRIMARY KEY(source, rel_path)
);

CREATE TABLE IF NOT EXISTS session(
  id                TEXT PRIMARY KEY,   -- '<source>:<session_key>'
  source            TEXT NOT NULL,
  harness           TEXT NOT NULL,
  project           TEXT,
  session_key       TEXT NOT NULL,
  native_id         TEXT,
  parent_session_id TEXT,               -- set for subagent/advisor sessions
  is_subagent       INTEGER DEFAULT 0,
  agent_name        TEXT,
  forked_from       TEXT,
  cwd               TEXT,
  title             TEXT,
  models            TEXT DEFAULT '[]',  -- JSON array, in order of appearance
  first_ts TEXT, last_ts TEXT,
  n_events INTEGER DEFAULT 0, n_user INTEGER DEFAULT 0,
  n_tool_calls INTEGER DEFAULT 0, n_tool_errors INTEGER DEFAULT 0,
  n_abandoned INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event(
  id              TEXT PRIMARY KEY,     -- '<raw_id[:24]>:<part>'
  session_id      TEXT NOT NULL REFERENCES session(id),
  raw_id          TEXT,
  seq             INTEGER NOT NULL,     -- source line number (order within session)
  part            INTEGER NOT NULL,     -- index within the source line
  ts              TEXT,
  kind            TEXT NOT NULL,
  entry_type      TEXT,
  entry_id        TEXT,
  parent_entry_id TEXT,
  role            TEXT,
  model           TEXT,
  tool_name       TEXT,
  tool_norm       TEXT,
  tool_call_id    TEXT,
  detail          TEXT,                 -- redacted first line of command/path/pattern
  is_error        INTEGER DEFAULT 0,
  abandoned       INTEGER DEFAULT 0,    -- 1 = on a branch the user rewound away from
  text            TEXT,                 -- redacted, truncated
  text_len        INTEGER               -- original length before truncation
);
CREATE INDEX IF NOT EXISTS event_by_session ON event(session_id, seq, part);
CREATE INDEX IF NOT EXISTS event_by_kind ON event(kind);
CREATE INDEX IF NOT EXISTS event_by_tool ON event(tool_norm);
"""

DEFAULT_REDACTIONS = [
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9]{36,}",
    r"github_pat_[A-Za-z0-9_]{40,}",
    r"sk-ant-[A-Za-z0-9_\-]{20,}",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"xox[abprs]-[A-Za-z0-9\-]{10,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{20,}",
]
# key = value style secrets: keep the key name, drop the value
KV_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:password|passwd|secret|api[_-]?key|access[_-]?key|token)[A-Z0-9_]*)"
    r"(\s*[:=]\s*[\"']?)([^\s\"']{8,})"
)


# ----------------------------------------------------------------------------- config
def load_config(path: Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("db", "data/archive.db")
    cfg.setdefault("max_line_bytes", 8_000_000)
    cfg.setdefault("max_file_bytes", 2_000_000_000)
    cfg.setdefault("max_text_chars", 20_000)
    cfg.setdefault("snapshot_keep", 5)
    cfg.setdefault("redact_patterns", [])
    for s in cfg.get("source", []):
        if s["harness"] not in REGISTRY:
            sys.exit(f"source {s['name']}: unknown harness {s['harness']!r} (known: {sorted(REGISTRY)})")
        s["root"] = Path(os.path.expanduser(s["root"]))
        s.setdefault("project", s["name"])
    names = [s["name"] for s in cfg.get("source", [])]
    if len(names) != len(set(names)):
        sys.exit("source names must be unique")
    return cfg


class Redactor:
    def __init__(self, extra: list[str]):
        self.patterns = [re.compile(p) for p in DEFAULT_REDACTIONS + list(extra)]

    def __call__(self, s: str | None) -> str | None:
        if not s:
            return s
        for p in self.patterns:
            s = p.sub("[REDACTED]", s)
        return KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", s)


def open_db(cfg: dict) -> sqlite3.Connection:
    p = (REPO / cfg["db"]).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    db = sqlite3.connect(p)
    os.chmod(p, 0o600)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    return db


# ----------------------------------------------------------------------------- discovery
def safe_files(root: Path):
    """Yield (rel_path, dirfd, name, stat) for regular files under root.

    os.fwalk + dir_fd-relative opens means a container swapping a directory for
    a symlink mid-walk can't redirect us outside the root. A root that is itself
    a symlink yields nothing.
    """
    if not root.exists():
        return
    for dirpath, _dirs, files, dirfd in os.fwalk(root, follow_symlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        for name in files:
            try:
                st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield (name if rel_dir == "." else f"{rel_dir}/{name}"), dirfd, name, st


def iter_lines(fh, start_line: int, max_line: int, budget: int, final: bool):
    """Yield (line_no, bytes) for complete lines. Returns (end_offset, line_count, skipped).

    A trailing line without '\\n' is left for the next run unless `final`
    (gz archives are complete). Lines over max_line are skipped, not truncated.
    """
    n, skipped, consumed = start_line, 0, 0
    pos = fh.tell()
    while consumed < budget:
        line = fh.readline(max_line + 1)
        if not line:
            break
        if not line.endswith(b"\n") and len(line) > max_line:      # overlong
            while True:
                chunk = fh.readline(1 << 20)
                if not chunk:
                    return pos, n, skipped                          # overlong + partial
                if chunk.endswith(b"\n"):
                    break
            pos, n, skipped = fh.tell(), n + 1, skipped + 1
            continue
        if not line.endswith(b"\n") and not final:
            break                                                   # still being written
        consumed += len(line)
        pos, n = fh.tell(), n + 1
        yield n, line
    return pos, n, skipped


# ----------------------------------------------------------------------------- ingest
class Ingester:
    def __init__(self, cfg: dict, db: sqlite3.Connection):
        self.cfg, self.db = cfg, db
        self.redact = Redactor(cfg["redact_patterns"])
        self.now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.touched: set[str] = set()
        self.counts = {"files": 0, "new_lines": 0, "events": 0, "skipped_long": 0, "bad_json": 0}

    # -- session bookkeeping
    def ensure_session(self, src: dict, info: KeyInfo) -> str:
        sid = f"{src['name']}:{info.session_key}"
        parent = f"{src['name']}:{info.parent_key}" if info.parent_key else None
        self.db.execute(
            "INSERT INTO session(id, source, harness, project, session_key, is_subagent, agent_name,"
            " parent_session_id) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (sid, src["name"], src["harness"], src["project"], info.session_key,
             int(info.is_subagent), info.agent_name, parent))
        return sid

    def apply_session_updates(self, src: dict, sid: str, agg: dict):
        if not agg:
            return
        parent = f"{src['name']}:{agg['parent_key']}" if agg.get("parent_key") else None
        self.db.execute(
            "UPDATE session SET native_id=COALESCE(?,native_id), cwd=COALESCE(?,cwd),"
            " title=COALESCE(?,title), forked_from=COALESCE(?,forked_from),"
            " parent_session_id=COALESCE(parent_session_id,?) WHERE id=?",
            (agg.get("native_id"), agg.get("cwd"), agg.get("title"), agg.get("forked_from"), parent, sid))
        if agg.get("models"):
            (cur,) = self.db.execute("SELECT models FROM session WHERE id=?", (sid,)).fetchone()
            models = json.loads(cur or "[]")
            models += [m for m in agg["models"] if m not in models]
            self.db.execute("UPDATE session SET models=? WHERE id=?", (json.dumps(models), sid))

    # -- per-line work, shared by `run` and `rebuild`
    def normalize_line(self, adapter, src, sid, ctx, agg, raw_id, line_no, line: str):
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError
        except ValueError:
            self.counts["bad_json"] += 1
            events, upd = [Event("other", entry_type="<invalid-json>")], {}
        else:
            try:
                upd, events = adapter.normalize(obj, ctx)
            except Exception as e:  # adapter bug must not stop ingestion; raw is kept
                upd, events = {}, [Event("other", entry_type=f"<adapter-error:{type(e).__name__}>")]
        for k, v in upd.items():
            if v is None:
                continue
            if k == "model":
                agg.setdefault("models", [])
                if v not in agg["models"]:
                    agg["models"].append(v)
            elif k != "ts":
                agg[k] = v
        maxc = self.cfg["max_text_chars"]
        rows = []
        for i, ev in enumerate(events):
            text = ev.text or ""
            rows.append((
                f"{raw_id[:24]}:{i}", sid, raw_id, line_no, i, ev.ts, ev.kind, ev.entry_type,
                ev.entry_id, ev.parent_entry_id, ev.role, ev.model, ev.tool_name,
                norm_tool(ev.tool_name), ev.tool_call_id, self.redact(ev.detail), ev.is_error,
                self.redact(text[:maxc]), len(text)))
        self.db.executemany(
            "INSERT OR IGNORE INTO event(id, session_id, raw_id, seq, part, ts, kind, entry_type,"
            " entry_id, parent_entry_id, role, model, tool_name, tool_norm, tool_call_id, detail,"
            " is_error, text, text_len) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.counts["events"] += len(rows)

    def ingest_file(self, src, adapter, rel, info: KeyInfo, dirfd, name, st):
        q = self.db.execute("SELECT dev, ino, size, mtime_ns, offset, line_count, head_len, head_hash"
                            " FROM file_state WHERE source=? AND rel_path=?", (src["name"], rel)).fetchone()
        is_gz = rel.endswith(".gz")
        if q and q[:4] == (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
            return                                                    # unchanged
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
        except OSError:
            return
        with os.fdopen(fd, "rb") as fh:
            fst = os.fstat(fh.fileno())
            if not stat.S_ISREG(fst.st_mode) or fst.st_ino != st.st_ino:
                return
            start_off, start_line = 0, 0
            if q and not is_gz and q[1] == fst.st_ino and fst.st_size >= q[4]:
                head = os.pread(fh.fileno(), q[6], 0)
                if hashlib.sha256(head).hexdigest() == q[7]:
                    start_off, start_line = q[4], q[5]                # pure append: resume
            self.counts["files"] += 1
            stream = gzip.GzipFile(fileobj=fh) if is_gz else fh
            if not is_gz:
                fh.seek(start_off)
            sid = self.ensure_session(src, info)
            ctx, agg = {"info": info}, {}
            if start_line:   # resuming: carry the model for pi-style sessions
                row = self.db.execute("SELECT models FROM session WHERE id=?", (sid,)).fetchone()
                ms = json.loads(row[0] or "[]") if row else []
                ctx["model"] = ms[-1] if ms else None
            gen = iter_lines(stream, start_line, self.cfg["max_line_bytes"],
                             self.cfg["max_file_bytes"], final=is_gz)
            try:
                while True:
                    line_no, b = next(gen)
                    line = b.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.strip():
                        continue
                    raw_id = hashlib.sha256(
                        f"{src['name']}\0{info.session_key}\0{line}".encode()).hexdigest()
                    cur = self.db.execute(
                        "INSERT OR IGNORE INTO raw(id, source, harness, session_key, line_no, line,"
                        " ingested_at) VALUES(?,?,?,?,?,?,?)",
                        (raw_id, src["name"], src["harness"], info.session_key, line_no, line, self.now))
                    if cur.rowcount:
                        self.counts["new_lines"] += 1
                        self.normalize_line(adapter, src, sid, ctx, agg, raw_id, line_no, line)
            except StopIteration as stop:
                end_off, end_line, skipped = stop.value
            except (OSError, EOFError, gzip.BadGzipFile):
                return                                                # corrupt/truncated gz
            self.counts["skipped_long"] += skipped
            self.apply_session_updates(src, sid, agg)
            self.touched.add(sid)
            head_len = min(HEAD_BYTES, end_off) if not is_gz else 0
            head_hash = hashlib.sha256(os.pread(fh.fileno(), head_len, 0)).hexdigest()
            self.db.execute(
                "INSERT OR REPLACE INTO file_state VALUES(?,?,?,?,?,?,?,?,?,?)",
                (src["name"], rel, fst.st_dev, fst.st_ino, fst.st_size, fst.st_mtime_ns,
                 end_off, end_line, head_len, head_hash))

    def run(self):
        for src in self.cfg.get("source", []):
            adapter = REGISTRY[src["harness"]]
            for rel, dirfd, name, st in safe_files(src["root"]):
                info = adapter.classify(rel)
                if info:
                    self.ingest_file(src, adapter, rel, info, dirfd, name, st)
            self.db.commit()
        self.finalize()

    # -- derived fields for touched sessions
    def finalize(self):
        db = self.db
        for sid in self.touched:
            db.execute(
                "UPDATE event SET tool_name=(SELECT c.tool_name FROM event c WHERE c.session_id=event.session_id"
                " AND c.kind='tool_call' AND c.tool_call_id=event.tool_call_id LIMIT 1),"
                " tool_norm=(SELECT c.tool_norm FROM event c WHERE c.session_id=event.session_id"
                " AND c.kind='tool_call' AND c.tool_call_id=event.tool_call_id LIMIT 1)"
                " WHERE session_id=? AND kind='tool_result' AND tool_name IS NULL AND tool_call_id IS NOT NULL",
                (sid,))
            self.mark_abandoned(sid)
            db.execute(
                "UPDATE session SET (first_ts, last_ts, n_events, n_user, n_tool_calls, n_tool_errors,"
                " n_abandoned) = (SELECT MIN(ts), MAX(ts), COUNT(*), SUM(kind='user'),"
                " SUM(kind='tool_call'), SUM(kind='tool_result' AND is_error), SUM(abandoned)"
                " FROM event WHERE session_id=?) WHERE id=?", (sid, sid))
        db.commit()

    def mark_abandoned(self, sid: str):
        """Events not on the path from the latest conversational entry to the root
        were rewound away from (edit-and-retry, /tree branch switch, CC rewind)."""
        rows = self.db.execute(
            "SELECT entry_id, parent_entry_id, kind FROM event WHERE session_id=? AND entry_id IS NOT NULL"
            " ORDER BY seq, part", (sid,)).fetchall()
        if not rows:
            return
        parent = {}
        leaf = None
        for eid, pid, kind in rows:
            parent.setdefault(eid, pid)
            if kind in ("user", "assistant", "tool_call", "tool_result", "user_shell", "compaction"):
                leaf = eid
        live, cur = set(), leaf
        while cur and cur not in live:
            live.add(cur)
            cur = parent.get(cur)
        self.db.execute("UPDATE event SET abandoned=0 WHERE session_id=?", (sid,))
        dead = [(sid, e) for e in parent if e not in live]
        self.db.executemany("UPDATE event SET abandoned=1 WHERE session_id=? AND entry_id=?"
                            " AND kind NOT IN ('meta','other')", dead)


# ----------------------------------------------------------------------------- commands
def cmd_rebuild(cfg, db):
    db.executescript("DELETE FROM event; DELETE FROM session;")
    ing = Ingester(cfg, db)
    srcs = {s["name"]: s for s in cfg.get("source", [])}
    cur = db.execute("SELECT DISTINCT source, session_key FROM raw")
    for source, key in cur.fetchall():
        src = srcs.get(source)
        if not src:
            print(f"skip {source}:{key} (source no longer configured; raw kept)", file=sys.stderr)
            continue
        adapter = REGISTRY[src["harness"]]
        # re-derive KeyInfo from the key: adapters classify paths, so rebuild a plausible rel path
        info = adapter.classify(f"{key}.jsonl") or adapter.classify(f"sessions/{key}.jsonl") \
            or KeyInfo(key)
        sid = ing.ensure_session(src, info)
        ctx, agg = {"info": info}, {}
        for raw_id, line_no, line in db.execute(
                "SELECT id, line_no, line FROM raw WHERE source=? AND session_key=? ORDER BY line_no, ingested_at",
                (source, key)).fetchall():
            ing.normalize_line(adapter, src, sid, ctx, agg, raw_id, line_no, line)
        ing.apply_session_updates(src, sid, agg)
        ing.touched.add(sid)
    db.commit()
    ing.finalize()
    print(json.dumps(ing.counts))


def cmd_snapshot(cfg, db):
    out = (REPO / cfg["db"]).resolve().parent / "snapshots"
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final, tmp = out / f"analysis-{ts}.db", out / f".analysis-{ts}.db.tmp"
    db.execute("VACUUM INTO ?", (str(tmp),))
    s = sqlite3.connect(tmp)
    s.executescript("""
      DROP TABLE raw; DROP TABLE file_state;
      UPDATE event SET raw_id = NULL;
      CREATE VIRTUAL TABLE event_fts USING fts5(text, content='event', tokenize='porter unicode61');
      INSERT INTO event_fts(event_fts) VALUES('rebuild');
      CREATE TABLE snapshot_info(created_at TEXT, n_sessions INTEGER, n_events INTEGER);
    """)
    s.execute("INSERT INTO snapshot_info SELECT ?, (SELECT COUNT(*) FROM session), (SELECT COUNT(*) FROM event)",
              (ts,))
    s.commit()
    s.execute("PRAGMA journal_mode=DELETE")   # no -wal/-shm: safe to mount read-only
    s.execute("VACUUM")
    s.close()
    os.chmod(tmp, 0o644)   # parent dir is 0700; the container reads it via its bind mount
    os.replace(tmp, final)
    link = out / "latest.db"
    tmp_link = out / ".latest.tmp"
    if tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(final.name)
    os.replace(tmp_link, link)
    snaps = sorted(out.glob("analysis-*.db"))
    for old in snaps[: -cfg["snapshot_keep"]]:
        old.unlink()
    print(final)


def cmd_stats(cfg, db):
    q = lambda sql: db.execute(sql).fetchall()  # noqa: E731
    print("sessions by harness/project:")
    for r in q("SELECT harness, project, COUNT(*), SUM(is_subagent), MAX(last_ts) FROM session"
               " GROUP BY 1,2 ORDER BY 1,2"):
        print(f"  {r[0]:12} {r[1]:24} sessions={r[2]:<5} subagents={r[3]:<5} last={r[4]}")
    print("events by model (assistant/tool_call):")
    for r in q("SELECT COALESCE(model,'?'), COUNT(*) FROM event WHERE kind IN ('assistant','tool_call')"
               " GROUP BY 1 ORDER BY 2 DESC LIMIT 20"):
        print(f"  {r[0]:40} {r[1]}")
    print("events by kind:")
    for r in q("SELECT kind, COUNT(*) FROM event GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r[0]:16} {r[1]}")
    unk = q("SELECT s.harness, e.entry_type, COUNT(*) FROM event e JOIN session s ON s.id=e.session_id"
            " WHERE e.kind='other' GROUP BY 1,2 ORDER BY 3 DESC")
    print("UNMAPPED entry types (extend the adapter, then `rebuild`):" if unk else "no unmapped entry types")
    for r in unk:
        print(f"  {r[0]:12} {r[1]!s:40} {r[2]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "rebuild", "snapshot", "stats"])
    ap.add_argument("-c", "--config", default=str(REPO / "config.toml"))
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    lock_path = (REPO / cfg["db"]).resolve().parent / ".ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another ingest.py is running")
        db = open_db(cfg)
        if args.command == "run":
            ing = Ingester(cfg, db)
            ing.run()
            print(json.dumps(ing.counts))
        elif args.command == "rebuild":
            cmd_rebuild(cfg, db)
        elif args.command == "snapshot":
            cmd_snapshot(cfg, db)
        else:
            cmd_stats(cfg, db)
        db.close()


if __name__ == "__main__":
    main()
