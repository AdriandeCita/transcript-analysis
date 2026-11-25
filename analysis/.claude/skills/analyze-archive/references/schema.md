# Snapshot schema (`/data/snapshots/latest.db`)

A read-only snapshot produced on the host by `ingest.py snapshot`. Raw lines are
not included; text is redacted and truncated (`text_len` holds the original length).

## session
One row per transcript file (main sessions, subagents and advisors are separate sessions).

| column | meaning |
|---|---|
| `id` | `<source>:<session_key>`; stable, use it everywhere |
| `source`, `project` | configured source name / project label |
| `harness` | `claude_code`, `omp`, `pi`, ... |
| `native_id` | harness's own session id |
| `parent_session_id` | for subagents/advisors: the session that spawned it |
| `is_subagent`, `agent_name` | subagent flag and name (`advisor:<name>` for OMP advisors) |
| `forked_from` | pi/OMP `/fork` origin (a file path from the container) |
| `cwd`, `title` | working dir inside the container; harness-generated title if any |
| `models` | JSON array of models seen, in order (`json_extract(models,'$[#-1]')` = last) |
| `first_ts`, `last_ts` | ISO-8601 |
| `n_events`, `n_user`, `n_tool_calls`, `n_tool_errors`, `n_abandoned` | counts |

## event
One row per normalized event. A single source line can yield several events
(an assistant message with text + 2 tool calls = 3 events, same `seq`, `part` 0..2).

| column | meaning |
|---|---|
| `session_id` | → `session.id` |
| `seq`, `part` | order within the session: `ORDER BY seq, part` |
| `ts` | ISO-8601, may be NULL for bookkeeping lines |
| `kind` | see below |
| `entry_type` | the harness's native line type (diagnostics) |
| `entry_id`, `parent_entry_id` | native tree links (pi/OMP `id/parentId`, Claude Code `uuid/parentUuid`) |
| `abandoned` | 1 = on a branch the user rewound away from (edit-and-retry, `/tree`, rewind) |
| `role` | `user`/`assistant` where meaningful |
| `model` | model that produced this assistant/thinking/tool_call event |
| `tool_name` | harness-specific (`Bash`, `bash`, `MultiEdit`, `mcp__x__y`) |
| `tool_norm` | harness-independent family: `bash`, `read`, `edit`, `write`, `grep`, `glob`, `list`, `web_fetch`, `web_search`, `subagent`, `todo`, or the lowercased name |
| `tool_call_id` | joins a `tool_call` to its `tool_result` within a session |
| `detail` | first line of the main argument: shell command, file path, pattern, URL |
| `is_error` | tool result failed / shell exit code ≠ 0 / provider error |
| `text`, `text_len` | content (tool_call: JSON arguments) and original length |

### kind values
`user` (human prompt), `user_shell` (shell command the human ran directly in pi/OMP),
`assistant`, `thinking`, `tool_call`, `tool_result`, `system` (harness-injected),
`compaction`, `branch_summary`, `model_change`, `meta` (bookkeeping), `other` (adapter didn't recognize it).

### Gotchas
- Claude Code slash commands appear as `user` text starting with `<command-` or `<local-command`. Exclude them from prompt analysis.
- Subagent `user` messages are written by the parent model, not the human. Filter `session.is_subagent = 0` for human-behaviour questions.
- `tool_result.model` is NULL; join to the `tool_call` via `tool_call_id` to attribute errors to a model.
- Tool sets differ by harness: compare on `tool_norm`, not `tool_name`.

## event_fts
FTS5 index over `event.text` (porter stemming). Join on rowid:
```sql
SELECT e.* FROM event_fts f JOIN event e ON e.rowid = f.rowid WHERE event_fts MATCH '"uv run" AND pytest';
```

## snapshot_info
`created_at`, `n_sessions`, `n_events`: put `created_at` in the report's Scope section.
