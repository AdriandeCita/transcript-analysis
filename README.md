# agent-archive

Collects coding-agent transcripts (Claude Code, OMP, pi) from isolated devcontainers into one SQLite archive on the host. The transcripts are then analyzed in a separate, firewalled container to find skill candidates, project rules, and model/harness insights. All output is a draft for a human to review. Nothing is applied automatically.

Python 3.11+, stdlib only. The design, trust model, and details are in [SPEC.md](SPEC.md). This file is the setup guide and the to-do list.

## Status

| Part | State |
|---|---|
| Ingester (`ingest/`: `run`, `stats`, `rebuild`, `snapshot`) | Implemented, smoke-tested on synthetic Claude Code + OMP data |
| Analysis skill (`analysis/.claude/skills/analyze-archive/`) | Implemented; all named queries run on a snapshot |
| systemd timer (`deploy/`) | Written, not yet installed anywhere |
| Analysis devcontainer (`analysis/.devcontainer/`) | **Missing** (sketch in SPEC §8.1) |
| Source auto-discovery | Specified (SPEC §4.4), not implemented |
| Transcript preview (`preview/`) | Specified (SPEC §10), not implemented |
| Real-data validation | Not done yet |

## Setup

All of this runs on the **host**, not inside a devcontainer.

### 1. Clone and configure
```bash
git clone <repo> ~/src/agent-archive      # the systemd unit assumes this path
cd ~/src/agent-archive
cp config.example.toml config.toml        # gitignored; list your projects as [[source]]
```

### 2. Mount each agent's transcripts to the host
Each agent devcontainer bind-mounts its harness state to `~/agent-data/<project>/` (SPEC §4):

```jsonc
"initializeCommand": "mkdir -p ${localEnv:HOME}/agent-data/${localWorkspaceFolderBasename}/claude",
"mounts": [
  "source=${localEnv:HOME}/agent-data/${localWorkspaceFolderBasename}/claude,target=/home/node/.claude,type=bind"
]
```
- This replaces the template's `claude-code-config-${devcontainerId}` named volume. Copy the old volume's `projects/` across once (command in SPEC §4.4).
- Set `"cleanupPeriodDays": 3650` in that `~/.claude/settings.json`, otherwise Claude Code deletes transcripts after about 30 days.
- OMP/pi: mount `~/agent-data/<project>/omp-agent` to `/home/node/.omp/agent` the same way.

### 3. First ingest
```bash
python3 ingest/ingest.py run
python3 ingest/ingest.py stats     # check "UNMAPPED entry types": extend adapters.py if needed, then `rebuild`
```

### 4. Schedule it
```bash
mkdir -p ~/.config/systemd/user
cp deploy/agent-archive-ingest.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-archive-ingest.timer
journalctl --user -u agent-archive-ingest -f
```

### 5. Run an analysis
Requires the analysis devcontainer (see To-do).
```bash
python3 ingest/ingest.py snapshot
mkdir -p out/runs
devcontainer up --workspace-folder analysis
devcontainer exec --workspace-folder analysis claude    # "run the archive analysis"
```
Review `out/runs/<date>/report.md`, then promote skills and rules by hand (SPEC §8.3).

### Routine operations
| When | Do |
|---|---|
| Upgraded a harness | `run` + `stats`, check for new unmapped types |
| Changed an adapter or `redact_patterns` | `rebuild` + `snapshot` |
| Backup | `sqlite3 data/archive.db ".backup '/backup/archive-$(date +%F).db'"` (the only irreplaceable file; contains unredacted transcripts, so don't sync `data/` to cloud folders) |

## To-do

### Needed for a first real run
- [ ] Host: clone to `~/src/agent-archive` (or edit `WorkingDirectory` in the service), create `config.toml`
- [ ] Switch agent devcontainers from named volumes to `~/agent-data` bind mounts; migrate existing `projects/`; set `cleanupPeriodDays`
- [ ] First `run` + `stats` on real data. Adapters have only been exercised against synthetic lines, so extend them for any unmapped types
- [ ] Install and enable the systemd timer
- [ ] Build the analysis devcontainer in `analysis/.devcontainer/`: `devcontainer.json` (SPEC §8.1), Dockerfile with python3, and a firewall allowlisting only the model API (the repo's own `.devcontainer/` firewall is broader and not suitable as-is)
- [ ] Snapshot access from the analysis container: run it with the host UID (`updateRemoteUserUID`, the Linux default), because `ingest.py` keeps `data/` at `0700` (SPEC §8.1)

### Planned
- [ ] Source auto-discovery via `[[discover]]` patterns (SPEC §4.4)
- [ ] `preview/build.py` static transcript viewer (SPEC §10). Until then, use `datasette serve -i data/snapshots/latest.db -h 127.0.0.1`
- [ ] Cross-file dedup for Claude Code `--resume`/`--continue` sessions (SPEC §11)
- [ ] Optional: inotify trigger, prompt clustering with `sqlite-vec`, push-mode receiver, Codex CLI / OpenCode adapters (SPEC §11)

### Done
- [x] Ingester, adapters (Claude Code, pi, OMP), redaction, snapshot
- [x] Analysis skill + query library + read-only `q.py`
- [x] Fix: `snapshot` now writes next to the configured `db` instead of always `<repo>/data/snapshots`
- [x] Fix: `SKILL.md` looked for previous reports in `/out/insights/` instead of `/out/*/report.md`
