-- Query library for analyze-archive. Each block starts with "-- name: <id>".
-- Run one block:   python3 scripts/q.py <id> [--param value ...]
-- Defaults: :since = '0000', :min_sessions = 3. All queries skip abandoned
-- branches unless the query is about them, and exclude subagent sessions
-- where noted (subagent prompts are model-written, not yours).

-- name: overview
-- Volume per harness / project / model. Run first to scope the analysis.
SELECT s.harness, s.project, json_extract(s.models, '$[0]') AS first_model,
       COUNT(*) AS sessions, SUM(s.is_subagent) AS subagents,
       SUM(s.n_user) AS user_msgs, SUM(s.n_tool_calls) AS tool_calls,
       ROUND(1.0 * SUM(s.n_tool_errors) / MAX(SUM(s.n_tool_calls), 1), 3) AS tool_err_rate,
       MIN(s.first_ts) AS first, MAX(s.last_ts) AS last
FROM session s
WHERE COALESCE(s.last_ts, '') >= :since
GROUP BY 1, 2, 3 ORDER BY sessions DESC;

-- name: repeated_openers
-- First ~100 chars of human prompts that recur across sessions.
-- Recurring openers = workflows you start the same way every time -> skill / command candidates.
SELECT LOWER(TRIM(SUBSTR(e.text, 1, 100))) AS opener,
       COUNT(DISTINCT e.session_id) AS sessions,
       COUNT(DISTINCT s.project) AS projects,
       GROUP_CONCAT(DISTINCT s.harness) AS harnesses,
       MIN(e.session_id) AS example_session
FROM event e JOIN session s ON s.id = e.session_id
WHERE e.kind = 'user' AND e.abandoned = 0 AND s.is_subagent = 0
  AND e.text NOT LIKE '<command-%' AND e.text NOT LIKE '<local-command%'
  AND LENGTH(e.text) > 15 AND COALESCE(e.ts, '') >= :since
GROUP BY opener HAVING sessions >= :min_sessions
ORDER BY sessions DESC LIMIT 50;

-- name: standing_instructions
-- Human messages phrased as rules ("always", "never", "make sure", "remember").
-- Rules you restate across sessions belong in CLAUDE.md / AGENTS.md or a skill.
SELECT e.session_id, s.project, s.harness, e.ts, SUBSTR(e.text, 1, 400) AS text
FROM event_fts f JOIN event e ON e.rowid = f.rowid JOIN session s ON s.id = e.session_id
WHERE event_fts MATCH '"always" OR "never" OR "make sure" OR "remember" OR "don''t" OR "do not" OR "instead of"'
  AND e.kind = 'user' AND e.abandoned = 0 AND s.is_subagent = 0
  AND COALESCE(e.ts, '') >= :since
ORDER BY s.project, e.ts LIMIT 300;

-- name: corrections
-- Human message directly following model output that pushes back.
-- Each row is a moment the agent did something you had to undo or redirect.
WITH ordered AS (
  SELECT e.*, LAG(e.kind) OVER (PARTITION BY e.session_id ORDER BY e.seq, e.part) AS prev_kind
  FROM event e WHERE e.abandoned = 0 AND e.kind NOT IN ('meta', 'thinking')
)
SELECT o.session_id, s.project, s.harness, json_extract(s.models, '$[#-1]') AS model,
       o.ts, SUBSTR(o.text, 1, 300) AS text
FROM ordered o JOIN session s ON s.id = o.session_id
WHERE o.kind = 'user' AND s.is_subagent = 0
  AND o.prev_kind IN ('assistant', 'tool_result', 'tool_call')
  AND COALESCE(o.ts, '') >= :since
  AND (LOWER(o.text) GLOB 'no[ ,.!]*' OR LOWER(o.text) GLOB 'nope*' OR LOWER(o.text) GLOB 'stop*'
       OR LOWER(o.text) GLOB 'wait*' OR LOWER(o.text) GLOB 'actually*' OR LOWER(o.text) GLOB 'wrong*'
       OR LOWER(o.text) LIKE '%that''s not%' OR LOWER(o.text) LIKE '%i said%'
       OR LOWER(o.text) LIKE '%i told you%' OR LOWER(o.text) LIKE '%don''t %'
       OR LOWER(o.text) LIKE '%instead%' OR LOWER(o.text) LIKE '%revert%' OR LOWER(o.text) LIKE '%undo%')
ORDER BY o.ts LIMIT 300;

-- name: rewinds
-- First live prompt after a stretch of abandoned (rewound) events, paired with the
-- abandoned prompt it replaced. The diff between the two is what you had to clarify.
WITH live AS (
  SELECT e.session_id, e.seq, e.ts, e.text,
         (SELECT MAX(a.seq) FROM event a WHERE a.session_id = e.session_id
            AND a.abandoned = 1 AND a.seq < e.seq) AS dead_seq
  FROM event e JOIN session s ON s.id = e.session_id
  WHERE e.kind = 'user' AND e.abandoned = 0 AND s.n_abandoned > 0 AND s.is_subagent = 0
    AND COALESCE(e.ts, '') >= :since
)
SELECT s.project, s.harness, l.session_id, l.ts, SUBSTR(l.text, 1, 300) AS retry_prompt,
       (SELECT SUBSTR(a.text, 1, 300) FROM event a WHERE a.session_id = l.session_id
          AND a.abandoned = 1 AND a.kind = 'user' AND a.seq <= l.dead_seq
        ORDER BY a.seq DESC LIMIT 1) AS abandoned_prompt
FROM live l JOIN session s ON s.id = l.session_id
WHERE l.dead_seq IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM event u WHERE u.session_id = l.session_id AND u.kind = 'user'
                    AND u.abandoned = 0 AND u.seq > l.dead_seq AND u.seq < l.seq)
ORDER BY l.ts LIMIT 200;

-- name: tool_trigrams
-- Recurring 3-step tool sequences (harness-independent tool families + command head).
-- High session/project counts = mechanical loops worth scripting into a skill.
WITH calls AS (
  SELECT e.session_id,
         e.tool_norm || CASE WHEN e.tool_norm = 'bash' AND e.detail IS NOT NULL
              THEN '(' || SUBSTR(e.detail, 1, INSTR(e.detail || ' ', ' ') - 1) || ')' ELSE '' END AS step,
         ROW_NUMBER() OVER (PARTITION BY e.session_id ORDER BY e.seq, e.part) AS rn
  FROM event e WHERE e.kind = 'tool_call' AND e.abandoned = 0 AND COALESCE(e.ts, '') >= :since
)
SELECT a.step || ' > ' || b.step || ' > ' || c.step AS sequence,
       COUNT(*) AS occurrences, COUNT(DISTINCT a.session_id) AS sessions,
       COUNT(DISTINCT s.project) AS projects
FROM calls a
JOIN calls b ON b.session_id = a.session_id AND b.rn = a.rn + 1
JOIN calls c ON c.session_id = a.session_id AND c.rn = a.rn + 2
JOIN session s ON s.id = a.session_id
GROUP BY sequence HAVING sessions >= :min_sessions
ORDER BY sessions DESC, occurrences DESC LIMIT 50;

-- name: bash_commands
-- Most frequent shell command heads (model- and human-run). Long tails of the
-- same command with different flags often hide a missing project convention.
SELECT e.kind, SUBSTR(e.detail, 1, 60) AS command, COUNT(*) AS n,
       COUNT(DISTINCT e.session_id) AS sessions, SUM(r.is_error) AS errors
FROM event e
LEFT JOIN event r ON r.session_id = e.session_id AND r.kind = 'tool_result' AND r.tool_call_id = e.tool_call_id
WHERE ((e.kind = 'tool_call' AND e.tool_norm = 'bash') OR e.kind = 'user_shell')
  AND e.abandoned = 0 AND COALESCE(e.ts, '') >= :since
GROUP BY 1, 2 HAVING sessions >= :min_sessions
ORDER BY sessions DESC LIMIT 60;

-- name: failure_loops
-- Runs of >= 3 consecutive failing tool results in one session, by model.
-- Separates capability gaps (small models) from environment/tooling problems (all models).
WITH results AS (
  SELECT e.session_id, e.seq, e.is_error, e.tool_norm,
         ROW_NUMBER() OVER (PARTITION BY e.session_id ORDER BY e.seq, e.part)
         - ROW_NUMBER() OVER (PARTITION BY e.session_id, e.is_error ORDER BY e.seq, e.part) AS grp
  FROM event e WHERE e.kind = 'tool_result' AND e.abandoned = 0 AND COALESCE(e.ts, '') >= :since
)
SELECT r.session_id, s.project, s.harness, json_extract(s.models, '$[#-1]') AS model,
       COUNT(*) AS consecutive_errors, GROUP_CONCAT(DISTINCT r.tool_norm) AS tools, MIN(r.seq) AS from_seq
FROM results r JOIN session s ON s.id = r.session_id
WHERE r.is_error = 1
GROUP BY r.session_id, r.grp HAVING consecutive_errors >= 3
ORDER BY consecutive_errors DESC LIMIT 100;

-- name: model_scorecard
-- Per-model behaviour on comparable work. Treat as directional, not a benchmark:
-- task mix differs per model. Use to decide where small models are good enough.
SELECT e.model, s.harness,
       COUNT(DISTINCT e.session_id) AS sessions,
       SUM(e.kind = 'tool_call') AS tool_calls,
       ROUND(1.0 * (SELECT SUM(r.is_error) FROM event r
                    JOIN event c ON c.session_id = r.session_id AND c.tool_call_id = r.tool_call_id
                    WHERE r.kind = 'tool_result' AND c.kind = 'tool_call' AND c.model = e.model)
             / MAX(SUM(e.kind = 'tool_call'), 1), 3) AS tool_err_rate,
       ROUND(1.0 * SUM(e.kind = 'tool_call') / COUNT(DISTINCT e.session_id), 1) AS calls_per_session,
       ROUND(1.0 * SUM(e.abandoned) / COUNT(*), 3) AS abandoned_share
FROM event e JOIN session s ON s.id = e.session_id
WHERE e.model IS NOT NULL AND e.kind IN ('assistant', 'tool_call') AND COALESCE(e.ts, '') >= :since
GROUP BY e.model, s.harness ORDER BY sessions DESC;

-- name: session_timeline
-- Full readable timeline of one session (params: --session '<id>').
-- Use to read evidence in context before claiming a pattern.
SELECT e.seq, e.kind, e.abandoned AS ab, e.model, e.tool_norm, e.is_error AS err,
       SUBSTR(COALESCE(e.detail, ''), 1, 80) AS detail, SUBSTR(e.text, 1, 600) AS text
FROM event e WHERE e.session_id = :session AND e.kind NOT IN ('meta')
ORDER BY e.seq, e.part;

-- name: search
-- Full-text search across all event text (params: --q 'fts5 query').
SELECT e.session_id, s.project, e.kind, e.ts, SUBSTR(e.text, 1, 300) AS text
FROM event_fts f JOIN event e ON e.rowid = f.rowid JOIN session s ON s.id = e.session_id
WHERE event_fts MATCH :q AND e.kind <> 'meta'
ORDER BY rank LIMIT 100;
