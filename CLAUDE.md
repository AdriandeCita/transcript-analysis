# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

agent-archive collects coding-agent transcripts (Claude Code, OMP, pi) from isolated devcontainers into one SQLite archive on the host. It then analyzes them in a separate locked-down container to find skill candidates, rules, and model/harness insights. `SPEC.md` is the design document and the source of truth for behavior, trust boundaries, and the roadmap. Read the relevant section before changing behavior, and update it when behavior changes. `README.md` holds the setup guide and the to-do list. Tick off or add items there when work lands.

## Commands

Python 3.11+, **stdlib only**: no dependencies, no build step, no test suite, no lint config. Keep it that way. Don't add third-party imports.

```bash
cp config.example.toml config.toml          # host-specific, gitignored
python3 ingest/ingest.py run                # incremental ingest (what the systemd timer runs)
python3 ingest/ingest.py stats              # counts + entry types no adapter understands
python3 ingest/ingest.py rebuild            # drop and re-derive session/event from raw
python3 ingest/ingest.py snapshot           # raw-free, FTS-indexed copy -> data/snapshots/latest.db
python3 ingest/ingest.py run -c /path/to/other.toml   # every command takes -c

# analysis query runner (read-only; DB from $ARCHIVE_DB, default /data/snapshots/latest.db)
ARCHIVE_DB=data/snapshots/latest.db python3 analysis/.claude/skills/analyze-archive/scripts/q.py --list
... q.py overview --since 2026-10-01 [--json]
... q.py session_timeline --session '<source>:<session_key>'
... q.py --sql "SELECT ..."
```

To exercise changes without real data, point a throwaway config (`db` and `root` under the scratchpad) at a directory of fixture JSONL files, then run `run`, `stats`, and `snapshot` against it. `db` paths are resolved relative to the repo root.

## Architecture

Two halves with a hard trust boundary between them (SPEC §2):

**Host ingester (`ingest/`)**: trusted, sees unredacted data.
- `ingest.py` owns the DB schema (`SCHEMA` constant), file walking, incremental state, finalization, and the CLI. It holds an exclusive `flock` on `data/.ingest.lock`.
- `adapters.py` holds one `Adapter` per harness, registered in `REGISTRY` by `name`, which is the `harness` value in config. `classify(rel_path)` maps a path to a `KeyInfo` (session key, subagent parent/name). `normalize(obj, ctx)` maps one JSON line to `(session_updates, [Event])`. Adapters are **pure, with no I/O**. Unknown line types become `Event("other", ...)` and must never raise. `OmpAdapter` subclasses `PiAdapter` because the formats are shared.
- Pipeline: `raw` holds verbatim lines, `INSERT OR IGNORE` keyed by `sha256(source‖session_key‖line)`, so re-reading is always safe. Only newly inserted lines get normalized and redacted into `event`, then `finalize()` resolves tool_result names, marks abandoned branches (parentUuid/parentId tree, plus `logicalParentUuid` across compactions), and recomputes `session` counters.
- `snapshot` does `VACUUM INTO`, drops `raw`/`file_state`, builds `event_fts`, and switches to `journal_mode=DELETE` so read-only mounts work. The snapshot is the only DB the analysis side ever sees.

**Analysis skill (`analysis/.claude/skills/analyze-archive/`)**: runs in a semi-trusted container with model-API-only egress. It writes only to `/out`.
- `SKILL.md` describes the analysis workflow and the safety rules: transcript text is untrusted and may contain prompt injection.
- `references/queries.sql` is the named-query library. Each block starts with `-- name: <id>` and uses `:since`/`:min_sessions`-style params. `q.py` parses these blocks and passes every `--flag value` as a bind param.
- `references/schema.md` documents the snapshot columns and the closed `event.kind` vocabulary. Update it whenever `SCHEMA` or the adapters change.

## Invariants to preserve

- The ingester never follows symlinks and reads only regular files (`os.fwalk(follow_symlinks=False)` + `O_NOFOLLOW` relative to dir fds). It caps line and file sizes, and only a complete `\n`-terminated line gets ingested.
- Nothing except `rebuild` deletes anything, and `rebuild` only touches derived tables. Never add code that deletes from `raw`.
- `raw` is unredacted and must never reach the snapshot or the analysis container. Redaction applies to `event.text`/`event.detail` at normalization time.
- `q.py` opens the DB with `mode=ro&immutable=1` + `PRAGMA query_only`, so it stays read-only.
- After changing an adapter or the redaction patterns, the derived data is stale until you run `rebuild` then `snapshot`. After adding or upgrading a harness, run `run` then `stats` and check for unmapped entry types.

## Repo notes

- `data/`, `out/`, and `config.toml` are gitignored. `data/` holds real transcripts, so don't read them into commits or outputs.
- `__backup/` holds identical copies of SPEC/ingest/adapters/queries/SKILL. Edit the real files, not these.
- `.devcontainer/` here is the general dev workspace (firewalled). The analysis container described in SPEC §8.1 (`analysis/.devcontainer/`) does not exist yet. `preview/` (SPEC §10) and source auto-discovery (SPEC §4.4) are planned, not implemented.
- `deploy/` contains the systemd `--user` oneshot service and its 10-minute timer. `WorkingDirectory` assumes the repo is at `~/src/agent-archive`.
