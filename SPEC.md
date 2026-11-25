# agent-archive: specification

Collect coding-agent transcripts from isolated devcontainers into a single SQLite
archive on the host, and analyze them in a separate isolated container to find
workflow improvements, skill candidates and model/harness insights.

Status: v0.1 draft. Ingester and analysis skill are implemented; source auto-discovery (§4.4) and preview (§10) are specified.

---

## 1. Goals and non-goals

**Goals**
- Harness-agnostic: Claude Code and OMP today, pi and others later, via small adapters.
- Lossless: every transcript line is stored verbatim; normalization can be re-derived at any time.
- Tamper-resistant: no agent, however confused, can delete or alter what was already ingested, or reach another project's data.
- Minimal ops: stdlib Python + SQLite, one systemd timer, no daemons, no network services.
- Human in the loop: analysis produces drafts and reports; nothing is applied automatically.

**Non-goals**
- Real-time streaming or dashboards.
- Multi-host aggregation (could be added later by shipping snapshots).
- Automatic skill installation.

## 2. Trust boundaries

```
 ┌───────── devcontainer A (untrusted) ─────────┐   ┌──── devcontainer B (untrusted) ────┐
 │ Claude Code → ~/.claude/projects/*.jsonl     │   │ OMP → ~/.omp/agent/sessions/*.jsonl │
 └──────────────┬───────────────────────────────┘   └──────────────┬──────────────────────┘
                │ bind mount (rw for container)                    │
   ~/agent-data/A/claude                                ~/agent-data/B/omp-agent
                └────────────────┐                 ┌───────────────┘
                                 ▼                 ▼
 ┌──────────────────────────── HOST (trusted) ──────────────────────────────┐
 │ ingest.py run   (read-only, no symlinks, size caps, INSERT-only)          │
 │      ▼                                                                    │
 │ data/archive.db   raw (verbatim, unredacted) + session/event (redacted)   │
 │      ▼ ingest.py snapshot  (drops raw, adds FTS, journal_mode=DELETE)     │
 │ data/snapshots/analysis-<ts>.db  ← latest.db                              │
 └──────┬──────────────────────────────────────────────────────▲────────────┘
        │ ro mount                                             │ rw mount (out/runs only)
 ┌──────▼──────────── analysis container (semi-trusted) ───────┴────────────┐
 │ Claude Code + analyze-archive skill; egress: model API only              │
 └──────────────────────────────────────────────────────────────────────────┘
```

| Actor | Can | Cannot |
|---|---|---|
| Agent container | Read/write/delete its own transcripts | See other containers' data, the archive, or the host |
| Ingester (host) | Read source roots, write `data/` | Follow symlinks, read non-regular files, delete archive rows |
| Analysis container | Read snapshots, write `out/runs/`, call model API | See raw lines, write the archive, reach other hosts |

Residual risks, accepted:
- An agent can delete its own transcripts **before** the next ingest (window = timer interval).
- An agent can **write fake transcript lines** into its own mount. They are archived as that project's data. Analysis treats all text as untrusted (skill safety rules) and humans review every output.
- Redaction is regex-based and imperfect. The raw table stays on the host; only redacted text reaches the analysis container and the model API.

## 3. Repository layout

```
agent-archive/
├── SPEC.md                          this document
├── config.example.toml              template; copy to config.toml
├── config.toml                      ✗ gitignored: host-specific source list
├── .gitignore
├── ingest/                          HOST ONLY
│   ├── ingest.py                    CLI: run | rebuild | snapshot | stats
│   └── adapters.py                  per-harness classify() + normalize()
├── deploy/
│   ├── agent-archive-ingest.service systemd --user oneshot
│   └── agent-archive-ingest.timer   every 10 min
├── analysis/                        mounted read-only into the analysis container as /workspace
│   ├── .devcontainer/
│   │   ├── devcontainer.json        (to add; sketch in §8.1)
│   │   └── init-firewall.sh         (reuse your existing one, allowlist = model API)
│   └── .claude/skills/analyze-archive/
│       ├── SKILL.md
│       ├── scripts/q.py             read-only named-query runner
│       └── references/
│           ├── queries.sql          tested query library
│           └── schema.md
├── preview/                         (planned, §10) static transcript viewer generator
├── data/                            ✗ gitignored, 0700
│   ├── archive.db                   master archive (WAL), contains raw unredacted lines
│   ├── .ingest.lock
│   └── snapshots/
│       ├── analysis-<UTC ts>.db     last N kept (snapshot_keep)
│       └── latest.db → analysis-…   symlink
└── out/                             ✗ gitignored
    ├── runs/<YYYY-MM-DD>/           mounted rw into analysis container as /out
    │   ├── report.md
    │   ├── skills/<name>/{SKILL.md,EVIDENCE.md}
    │   └── rules/<project|user>.md
    └── preview/                     (planned) generated HTML
```

## 4. Source setup (per agent devcontainer)

Each project's devcontainer bind-mounts its harness state dir to a predictable
host path. One directory per project; containers never share a mount.

### 4.1 Claude Code
```jsonc
// project/.devcontainer/devcontainer.json
"mounts": [
  "source=${localEnv:HOME}/agent-data/${localWorkspaceFolderBasename}/claude,target=/home/node/.claude,type=bind"
]
```
- Source root in `config.toml`: `~/agent-data/<project>/claude/projects`
- **Set `"cleanupPeriodDays": 3650` in that `~/.claude/settings.json`.** Claude Code otherwise deletes transcripts after ~30 days. The archive would still hold anything ingested before that, but there's no reason to race it.
- If you migrate from a named volume, copy the old `projects/` across first. Ingestion is idempotent, so double ingestion is harmless.

### 4.2 OMP (and pi)
```jsonc
"mounts": [
  "source=${localEnv:HOME}/agent-data/${localWorkspaceFolderBasename}/omp-agent,target=/home/node/.omp/agent,type=bind"
]
```
- Source root: `~/agent-data/<project>/omp-agent` (the agent dir, which contains `sessions/` and `archive/`).
- Archived sessions (`archive/sessions/**.jsonl.gz`) map to the same session key as live ones and deduplicate.
- OMP also writes logs under `$HOME/.omp`. Those aren't transcripts; leave them in the container.
- pi: same adapter with `harness = "pi"`, mounting `~/.pi/agent`.

### 4.3 Root placement rule
A source `root` must be the mount point or a directory **inside** it. The
container fully controls everything below the mount point (it can replace
`projects/` with a symlink; the ingester then yields nothing for that source
rather than following it).

### 4.4 Source auto-discovery (planned)

Listing every project as a `[[source]]` gets tedious with many devcontainers.
Discovery derives sources from a standard host layout instead.

**Precondition: bind mounts, not named volumes.** The stock Claude Code devcontainer
template keeps `~/.claude` in a named volume (`claude-code-config-${devcontainerId}`).
That data lives under `/var/lib/docker/volumes/`, which needs root to read. The volume
name is a hash with no project name in it, so the volume can only be traced back to a
project through container labels, and that breaks once the container is removed.
Neither is acceptable for an unprivileged ingester. Every agent devcontainer
therefore uses the same bind-mount snippet (the §4.1/§4.2 layout, plus creating the
source dir up front, because the devcontainer CLI fails on a missing bind source):

```jsonc
"initializeCommand": "mkdir -p ${localEnv:HOME}/agent-data/${localWorkspaceFolderBasename}/claude",
"mounts": [
  "source=${localEnv:HOME}/agent-data/${localWorkspaceFolderBasename}/claude,target=/home/node/.claude,type=bind"
  // OMP/pi: add .../omp-agent -> /home/node/.omp/agent the same way (mkdir it too)
]
```

Migrating an existing project: copy the named volume's `projects/` into the new bind
source once (e.g. `docker run --rm -v claude-code-config-<id>:/v -v ~/agent-data/<project>/claude:/d alpine cp -a /v/projects /d/`).
Ingestion is idempotent, so overlapping copies are harmless. Note that OMP state
without any mount is lost on container rebuild today.

**Config:**
```toml
[[discover]]
pattern = "~/agent-data/*/claude/projects"   # exactly one '*', in a host-controlled segment
harness = "claude_code"

[[discover]]
pattern = "~/agent-data/*/omp-agent"
harness = "omp"
```

**Behaviour:**
- On every command, each `[[discover]]` pattern is expanded into sources. The `*`
  match becomes both `name` and `project`, and `root` is the expanded path. Matches
  that aren't directories are skipped.
- An explicit `[[source]]` with the same `name` overrides the discovered one (for a
  custom `project`, a different harness, or a root deeper inside the mount).
- Two discovered matches yielding the same name (e.g. the same basename from two
  patterns with different harnesses) are an **error**, not a merge. `name` is part of every
  session id, so silent merging would mix projects irrecoverably. Resolve it with an
  explicit `[[source]]`.
- `stats` lists discovered sources, so a new project showing up (or not) is visible.
- Security: the `*` must sit in a segment the host controls (`~/agent-data/<project>`
  is created by `initializeCommand` on the host, outside any container's mount), so a
  container can't add, rename or impersonate projects. Everything below the mount point
  is still container-controlled and handled by the §4.3 rules (a symlinked `projects/`
  yields nothing).
- Caveat inherited from `${localWorkspaceFolderBasename}`: two checkouts with the same
  folder name map to the same directory and therefore the same source. Rename one checkout or
  hard-code a distinct path in its devcontainer.

## 5. Ingestion (host)

### 5.1 Commands
| Command | Does |
|---|---|
| `python3 ingest/ingest.py run` | Incremental ingest of all sources (what the timer runs) |
| `... stats` | Sessions per harness/project, events per model and kind, **unmapped entry types** |
| `... rebuild` | Delete `session`/`event`, re-derive them from `raw` (after adapter or redaction changes) |
| `... snapshot` | Produce `data/snapshots/analysis-<ts>.db` and repoint `latest.db` |

All commands take `-c path/to/config.toml` and hold an exclusive lock; concurrent runs exit immediately.

### 5.2 Algorithm (`run`)
1. For each source, walk the root with `os.fwalk(follow_symlinks=False)` and open files relative to the directory fd with `O_NOFOLLOW`. Only regular files are considered, so symlinks, FIFOs and devices are skipped and directory-swap races can't redirect the walk.
2. `adapter.classify(rel_path)` decides whether a file is a transcript and derives a stable **session key** (plus subagent parent/name).
3. Skip unchanged files using `file_state` (dev, inode, size, mtime). For appended plain `.jsonl` files whose first ≤4 KiB hash still matches, resume from the stored byte offset. Otherwise re-read from the start; dedup makes this safe.
4. Read complete lines only. A trailing line without `\n` waits for the next run, and lines over `max_line_bytes` are skipped and counted. `.gz` files are read whole, capped by `max_file_bytes` decompressed.
5. `raw.id = sha256(source ‖ session_key ‖ line)` → `INSERT OR IGNORE`. Only newly inserted lines are normalized.
6. `adapter.normalize(obj, ctx)` → events + session updates. Text is redacted and truncated (`max_text_chars`) before insert. Adapter exceptions turn into `kind='other'` events; ingestion never stops on bad data.
7. Finalize touched sessions: resolve `tool_result` tool names via `tool_call_id`, mark **abandoned** events (not on the path from the latest conversational entry to the root, following `parentId`/`parentUuid`, and Claude Code's `logicalParentUuid` across compactions), and recompute session counters.

Nothing in `run` deletes from `raw`. The only destructive command is `rebuild`, and it only touches derived tables.

### 5.3 Data model
- `raw`: verbatim lines, unredacted, **never leaves the host**.
- `session`: one row per transcript file. Subagents/advisors are their own sessions with `parent_session_id`.
- `event`: normalized, one or more per raw line, with a closed `kind` vocabulary.

Full column reference: `analysis/.claude/skills/analyze-archive/references/schema.md` (the snapshot has the same tables minus `raw`/`file_state`).

### 5.4 Adapter contract
```python
class Adapter:
    name: str                                         # value of `harness` in config
    def classify(self, rel: str) -> KeyInfo | None    # path → session key / subagent info
    def normalize(self, obj: dict, ctx: dict) -> tuple[dict, list[Event]]
```
- Pure functions: no I/O. `ctx` is per-session scratch state (e.g. current model).
- Unknown line types → `Event("other", entry_type=...)`, never an exception.
- Session updates use keys `native_id`, `cwd`, `title`, `model`, `forked_from`, `parent_key`; `None` values are ignored.
- Adding a harness: write an adapter, register it in `REGISTRY`, add a `[[source]]`, then `run` and `stats`. Grow the adapter until `stats` shows no unmapped types you care about. Formats drift between harness releases, so make the `stats` check part of upgrading a harness.

Format references used for v0.1: Claude Code's `~/.claude/projects` JSONL (`uuid`/`parentUuid`, `message.content` blocks); the pi session format v3 (`docs/session-format.md` in pi-mono: header line, `id`/`parentId` tree, `message.role` ∈ user/assistant/toolResult/bashExecution), which OMP shares while adding subagent and advisor files next to the session.

### 5.5 Redaction
Applied to `event.text` and `event.detail` at normalization time: private key blocks, AWS/GitHub/Slack/Anthropic/OpenAI-style tokens, JWTs, bearer tokens, and `*key|secret|token|password* = value` pairs (key kept, value dropped). Extra patterns come from `redact_patterns` in config. After changing patterns, run `rebuild`, then `snapshot`.

### 5.6 Scheduling
```bash
mkdir -p ~/.config/systemd/user
cp deploy/agent-archive-ingest.{service,timer} ~/.config/systemd/user/
# edit WorkingDirectory if the repo isn't at ~/src/agent-archive
systemctl --user daemon-reload
systemctl --user enable --now agent-archive-ingest.timer
journalctl --user -u agent-archive-ingest -f
```
The 10-minute interval is the deletion window from §2. Shorten it if that matters to you; an unchanged run costs one `stat` per file.

### 5.7 Backups
`data/archive.db` is the only irreplaceable artifact. Back it up with
`sqlite3 data/archive.db ".backup '/backup/archive-$(date +%F).db'"`, or simply copy the latest snapshot plus the db while the timer is stopped. Don't sync `data/` to cloud folders: it contains unredacted transcripts.

## 6. Snapshot

`ingest.py snapshot`:
1. `VACUUM INTO` a temp file (consistent copy, master untouched).
2. Drop `raw` and `file_state`, null out `event.raw_id`.
3. Build `event_fts` (FTS5, porter stemming), add `snapshot_info`.
4. `journal_mode=DELETE` + `VACUUM`, so no `-wal`/`-shm` files are needed and read-only mounts work.
5. Atomic rename, repoint `latest.db`, prune to `snapshot_keep`.

The snapshot is the **only** database the analysis container ever sees.

## 7. Analysis skill

`analysis/.claude/skills/analyze-archive/`. Summary of what it does (details in its `SKILL.md`):
- Scope with `overview`, collect signals with the query library (`repeated_openers`, `standing_instructions`, `corrections`, `rewinds`, `tool_trigrams`, `bash_commands`, `failure_loops`, `model_scorecard`), then verify each candidate by reading ≥2 sessions with `session_timeline`.
- Classify into: skill / project rule / user-level rule / harness setting / model-routing note / environment fix.
- Evidence threshold: ≥3 sessions, ideally across projects or weeks. Small-model failure loops are capability data, never skill material.
- Output to `/out/<date>/`: `report.md`, `skills/<name>/{SKILL.md,EVIDENCE.md}`, `rules/*.md`. Skill drafts use the standard Agent Skills format so they work in Claude Code and pi-family harnesses alike.
- Safety: transcript text is untrusted (prompt-injection aware), write only to `/out`, never execute transcript commands, scrub residual secrets.

`scripts/q.py` opens the DB with `mode=ro&immutable=1` and `PRAGMA query_only`, so writes fail by construction.

## 8. Analysis container

### 8.1 Devcontainer (sketch)
Opened from `analysis/` (so `${localWorkspaceFolder}` = `analysis/`):
```jsonc
{
  "name": "agent-archive-analysis",
  "build": { "dockerfile": "Dockerfile" },          // base: your usual Claude Code devcontainer image + python3
  "runArgs": ["--cap-add=NET_ADMIN", "--cap-add=NET_RAW"],   // for init-firewall.sh
  "workspaceMount": "source=${localWorkspaceFolder},target=/workspace,type=bind,readonly",
  "workspaceFolder": "/workspace",
  "mounts": [
    "source=${localWorkspaceFolder}/../data/snapshots,target=/data/snapshots,type=bind,readonly",
    "source=${localWorkspaceFolder}/../out/runs,target=/out,type=bind",
    "source=agent-archive-analysis-claude,target=/home/node/.claude,type=volume"
  ],
  "containerEnv": {
    "ARCHIVE_DB": "/data/snapshots/latest.db",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  },
  "postStartCommand": "sudo /usr/local/bin/init-firewall.sh"
}
```
- **Firewall allowlist: the model API endpoint only** (`api.anthropic.com` for Claude Code). No package registries at runtime; bake dependencies into the image. The API is then the only egress path for (redacted) transcript content.
- `latest.db` is a relative symlink inside the mounted directory, so it resolves in the container.
- UID: snapshot files are `0644`, but `data/` is `0700` and `ingest.py` re-applies that mode to the DB directory on every run, so only the owning UID can traverse into `data/snapshots`. Run the container with your host UID (`"updateRemoteUserUID": true`, the default on Linux). Relaxing permissions on `data/snapshots` with a shared group does **not** work while `data/` stays `0700`. If a different UID is ever required, move snapshots out of `data/` (e.g. a separate `snapshot_dir` config key) rather than loosening `data/`.
- The analysis container's own transcripts live in its named volume and are **not** a configured source, which avoids the archive analyzing its own analysis runs. Add it as a source later if you want to study the analysis process itself.
- Model choice: run analysis with a strong model. Pattern judgment and "does this survive reading in context" are exactly where small models fail.

### 8.2 Running an analysis
```bash
python3 ingest/ingest.py run && python3 ingest/ingest.py snapshot   # host
mkdir -p out/runs
devcontainer up --workspace-folder analysis                          # or "Reopen in Container"
devcontainer exec --workspace-folder analysis claude                 # then: "run the archive analysis"
# focused variants: "analyze only OMP sessions since 2026-11-01", "compare qwen vs opus on edit failures"
```

### 8.3 Review and promotion (manual by design)
1. Read `out/runs/<date>/report.md`.
2. For each draft skill: read `EVIDENCE.md`, open a couple of the cited sessions in the preview (§10), edit `SKILL.md`.
3. Promote by copying into the target: project `.claude/skills/`, `~/.claude/skills/`, or the pi/OMP skills dir for that project. For rules, paste into that project's `CLAUDE.md` / `AGENTS.md`.
4. The next report's "Changes since last report" section shows whether a promoted skill or rule actually reduced the pattern.

Optional later: keep promoted skills in a separate git repo and mount it into agent containers read-only.

## 9. Operations checklist
- After upgrading a harness: `ingest.py run && ingest.py stats`, and check for new unmapped types.
- After editing an adapter or redaction patterns: `ingest.py rebuild && ingest.py snapshot`.
- Disk: raw roughly equals the sum of the JSONL files; event text is capped. `VACUUM` the master occasionally (stop the timer first).
- Removing a project: delete its `[[source]]`. Its data stays in `raw` (`rebuild` skips unconfigured sources but keeps their raw lines). To purge it: `DELETE FROM raw WHERE source='x'`, then `rebuild`.

## 10. Transcript preview (planned)

### 10.1 Existing tools usable today
| Tool | Covers | Use for | Caveat |
|---|---|---|---|
| **Datasette** (`datasette serve -i data/snapshots/latest.db -h 127.0.0.1`) | Any harness, normalized tables | Faceted browsing (harness/model/kind), SQL, FTS search, permalinks to query results | Tabular, not conversational; `-i` = immutable |
| **claude-code-transcripts** (`uvx claude-code-transcripts json <file.jsonl>`) | Claude Code raw JSONL | High-fidelity conversational HTML of a single session, thinking included | CC only; point it at host-side files under `~/agent-data/*/claude/projects` |
| **OMP built-in HTML export** | OMP sessions, subagents embedded | Inside the agent container | Per-session, manual |
| **ompweb** | OMP sessions | Browsing an OMP agent dir | It can **archive/delete** sessions. Only point it at a copy, never at live mounts or the archive |

Datasette + claude-code-transcripts is the interim setup. Neither gives a single harness-agnostic conversational view, which is the gap §10.2 fills.

### 10.2 `preview/build.py` (to implement)
Static site generator over a snapshot (redacted data only), no server required:

- **Input**: `--db data/snapshots/latest.db`. Selection: `--session <id>...`, `--from-report out/runs/<date>/report.md` (every session id cited in the report and EVIDENCE files), or `--sample N --seed S [--harness] [--model] [--project] [--since]` for random spot-checks.
- **Output**: `out/preview/index.html` + `out/preview/s/<urlsafe-session-id>.html`. Stable paths let reports link to `../../preview/s/<id>.html`.
- **Index**: table of selected sessions with harness, project, models, dates, counts (user / tool calls / errors / abandoned), title; client-side filter and sort (vanilla JS, data inlined as JSON).
- **Session page**:
  - Chat-style timeline ordered by `seq, part`; distinct styles per `kind`.
  - Tool calls collapsed to `tool_norm · detail`, expanding to arguments and the paired result; errors highlighted.
  - Thinking collapsed by default.
  - **Abandoned branches** shown inline, dimmed and collapsible, next to the prompt that replaced them (the most valuable review signal).
  - Compaction / branch summaries rendered as dividers.
  - Subagent sessions linked from the spawning call and back via `parent_session_id`.
  - Model changes shown as badges; truncated text marked with original length.
- **Constraints**: single self-contained files (inline CSS/JS, no CDN), so `file://` works and the preview works offline. All content HTML-escaped (transcripts contain arbitrary HTML/JS), with a strict CSP meta tag (`default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'`). Light/dark via `prefers-color-scheme`.
- **Stretch**: a per-session "reviewer note" field exported as JSON next to the run, so manual spot-check verdicts can feed back into the next analysis (`/out` already has a place for it).

## 11. Roadmap / open questions
- Source auto-discovery from `~/agent-data/*` (§4.4), after migrating devcontainers from named volumes to bind mounts.
- Cross-file dedup for Claude Code `--resume`/`--continue` sessions that replay history into a new file (currently stored per file; analysis may double-count).
- Optional inotify trigger to shrink the deletion window without shortening the timer.
- Embedding-based clustering of prompts (`sqlite-vec`) to catch rephrased repeats that prefix grouping misses.
- Push-mode alternative (harness hook → bind-mounted Unix socket → insert-only receiver) if the pull window ever proves insufficient.
- Codex CLI / OpenCode adapters when needed.
