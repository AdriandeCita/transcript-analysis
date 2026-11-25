#!/usr/bin/env python3
"""Run a named query from references/queries.sql against the analysis snapshot.

  q.py --list                          list query names
  q.py overview                        run with defaults (:since='0000', :min_sessions=3)
  q.py corrections --since 2026-10-01
  q.py session_timeline --session 'api:-work-api/abc'
  q.py search --q '"uv run" AND pytest'
  q.py --sql "SELECT ..."              ad-hoc read-only SQL
  add --json for JSON lines output (default: tab-separated with header)

The database is opened immutable/read-only: writes are impossible by construction.
DB path: $ARCHIVE_DB, default /data/snapshots/latest.db.
"""
import argparse
import json
import os
import re
import signal
import sqlite3
import sys
from pathlib import Path

QUERIES = Path(__file__).resolve().parent.parent / "references" / "queries.sql"
DEFAULTS = {"since": "0000", "min_sessions": 3}


def load_queries() -> dict[str, str]:
    blocks, name, buf = {}, None, []
    for line in QUERIES.read_text().splitlines():
        m = re.match(r"--\s*name:\s*(\S+)", line)
        if m:
            if name:
                blocks[name] = "\n".join(buf)
            name, buf = m.group(1), []
        elif name:
            buf.append(line)
    if name:
        blocks[name] = "\n".join(buf)
    return blocks


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sql")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-width", type=int, default=400, help="truncate cells in TSV output")
    args, extra = ap.parse_known_args()

    queries = load_queries()
    if args.list:
        print("\n".join(queries))
        return
    params = dict(DEFAULTS)
    it = iter(extra)
    for k in it:
        if not k.startswith("--"):
            sys.exit(f"unexpected argument {k}")
        v = next(it, None)
        params[k[2:].replace("-", "_")] = int(v) if v and v.isdigit() else v
    sql = args.sql or queries.get(args.name or "")
    if not sql:
        sys.exit(f"unknown query {args.name!r}; use --list")

    db_path = os.environ.get("ARCHIVE_DB", "/data/snapshots/latest.db")
    db = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    db.execute("PRAGMA query_only=1")
    used = set(re.findall(r":([A-Za-z_]\w*)", sql))
    missing = used - params.keys()
    if missing:
        sys.exit(f"missing parameters: {', '.join('--' + m for m in sorted(missing))}")
    cur = db.execute(sql, {k: params[k] for k in used})
    cols = [d[0] for d in cur.description]
    if args.json:
        for row in cur:
            print(json.dumps(dict(zip(cols, row)), ensure_ascii=False))
        return
    print("\t".join(cols))
    for row in cur:
        cells = ["" if v is None else str(v).replace("\n", " ").replace("\t", " ")[: args.max_width]
                 for v in row]
        print("\t".join(cells))


if __name__ == "__main__":
    main()
