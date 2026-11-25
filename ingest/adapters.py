"""Harness adapters.

An adapter knows two things about one harness:
  1. classify(rel_path)  -> which files under a source root are transcripts, and
                            how a file path maps to a stable session key
                            (plus subagent / parent relationships).
  2. normalize(obj, ctx) -> how one parsed JSONL line becomes zero or more
                            normalized Events, plus session-level updates.

Adapters must be pure: no I/O, no DB access. The raw line is always stored
verbatim by the core, so an adapter that misses a field loses nothing -- fix
the adapter and run `ingest.py rebuild`.

Unknown entry types must map to kind="other" (never raise), so `ingest.py stats`
can report what an adapter doesn't understand yet.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Event kinds (closed vocabulary -- the analysis skill relies on it):
#   user          human-authored prompt
#   user_shell    shell command the human ran directly (pi/OMP "!cmd")
#   assistant     model-visible answer text
#   thinking      reasoning text
#   tool_call     model requested a tool
#   tool_result   tool output returned to the model
#   system        harness-injected context
#   compaction    context summary replacing earlier history
#   branch_summary summary of an abandoned branch (pi/OMP /tree)
#   model_change  model switched mid-session
#   meta          bookkeeping lines (snapshots, labels, queue ops)
#   other         not understood by the adapter -> surfaced in `stats`
KINDS = {
    "user", "user_shell", "assistant", "thinking", "tool_call", "tool_result",
    "system", "compaction", "branch_summary", "model_change", "meta", "other",
}

TOOL_ALIASES = {
    "multiedit": "edit", "str_replace": "edit", "str_replace_based_edit_tool": "edit",
    "apply_patch": "edit", "notebookedit": "edit",
    "ls": "list", "find": "glob",
    "websearch": "web_search", "webfetch": "web_fetch", "fetch": "web_fetch",
    "task": "subagent", "agent": "subagent",
    "todowrite": "todo", "todoread": "todo",
}
DETAIL_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")


def norm_tool(name: str | None) -> str | None:
    """Harness-independent tool family: 'Bash'/'bash' -> 'bash', 'MultiEdit' -> 'edit'."""
    if not name:
        return None
    n = re.sub(r"[^a-z0-9_]", "", name.lower())
    return TOOL_ALIASES.get(n, n)


def detail_of(args) -> str | None:
    """First line of the most descriptive argument (command, path, pattern, ...)."""
    if isinstance(args, dict):
        for k in DETAIL_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().splitlines()[0][:200]
    return None


def text_of(content) -> str:
    """Flatten the many content shapes harnesses use into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return text_of([content])
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                t = p.get("type")
                if t in ("text", "input_text", "output_text"):
                    out.append(p.get("text") or "")
                elif t == "tool_result":
                    out.append(text_of(p.get("content")))
                elif t in ("image", "input_image"):
                    out.append("[image]")
        return "\n".join(x for x in out if x)
    return str(content)


@dataclass
class Event:
    kind: str
    entry_id: str | None = None          # native id of the source line (tree node)
    parent_entry_id: str | None = None   # native parent id (branching / rewinds)
    ts: str | None = None
    entry_type: str | None = None        # native line type, for adapter health stats
    role: str | None = None
    model: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    detail: str | None = None
    is_error: int = 0
    text: str = ""


@dataclass
class KeyInfo:
    session_key: str                     # stable across moves (e.g. OMP archive/.gz)
    is_subagent: bool = False
    parent_key: str | None = None
    agent_name: str | None = None


class Adapter:
    name = "base"

    def classify(self, rel: str) -> KeyInfo | None:
        raise NotImplementedError

    def normalize(self, obj: dict, ctx: dict) -> tuple[dict, list[Event]]:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Claude Code: <root>=~/.claude/projects
#   <enc-cwd>/<session-uuid>.jsonl
#   <enc-cwd>/<session-uuid>/subagents/agent-<id>.jsonl   (newer layout)
#   <enc-cwd>/agent-<id>.jsonl                             (older layout)
# --------------------------------------------------------------------------- #
class ClaudeCodeAdapter(Adapter):
    name = "claude_code"
    META_TYPES = {"file-history-snapshot", "queue-operation", "progress", "last-prompt"}

    def classify(self, rel: str) -> KeyInfo | None:
        if not rel.endswith(".jsonl"):
            return None
        key = rel[: -len(".jsonl")]
        parts = key.split("/")
        if "subagents" in parts:
            i = parts.index("subagents")
            return KeyInfo(key, True, "/".join(parts[:i]), parts[-1])
        if parts[-1].startswith("agent-"):
            return KeyInfo(key, True, None, parts[-1])   # parent resolved from sessionId
        return KeyInfo(key)

    def normalize(self, obj: dict, ctx: dict) -> tuple[dict, list[Event]]:
        t = obj.get("type")
        upd: dict = {}
        if obj.get("cwd"):
            upd["cwd"] = obj["cwd"]
        sid = obj.get("sessionId")
        if sid:
            if ctx["info"].is_subagent:
                if ctx["info"].parent_key is None:   # old flat layout
                    upd["parent_key"] = f"{ctx['info'].session_key.split('/')[0]}/{sid}"
            else:
                upd["native_id"] = sid
        if obj.get("timestamp"):
            upd["ts"] = obj["timestamp"]

        common = dict(entry_id=obj.get("uuid"), parent_entry_id=obj.get("parentUuid"),
                      ts=obj.get("timestamp"), entry_type=t)

        if t == "summary":
            upd["title"] = obj.get("summary")
            return upd, [Event("meta", entry_type=t, text=obj.get("summary") or "")]
        if t in self.META_TYPES:
            return upd, [Event("meta", **common)]
        if t == "system":
            if obj.get("subtype") == "compact_boundary":
                # compaction cuts parentUuid; logicalParentUuid keeps the chain intact
                common["parent_entry_id"] = obj.get("parentUuid") or obj.get("logicalParentUuid")
                return upd, [Event("compaction", **common, text=str(obj.get("content") or ""))]
            return upd, [Event("system", **common, text=str(obj.get("content") or ""))]

        msg = obj.get("message") or {}
        if t == "user":
            content = msg.get("content")
            if obj.get("isCompactSummary"):
                return upd, [Event("compaction", **common, text=text_of(content))]
            if obj.get("isMeta"):
                return upd, [Event("meta", **common, role="user", text=text_of(content))]
            evs = []
            rest = content
            if isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "tool_result":
                        evs.append(Event("tool_result", **common,
                                         tool_call_id=p.get("tool_use_id"),
                                         is_error=int(bool(p.get("is_error"))),
                                         text=text_of(p.get("content"))))
                rest = [p for p in content
                        if not (isinstance(p, dict) and p.get("type") == "tool_result")]
            txt = text_of(rest)
            if txt.strip():
                evs.append(Event("user", **common, role="user", text=txt))
            return upd, evs

        if t == "assistant":
            model = msg.get("model")
            if model and model != "<synthetic>":
                upd["model"] = model
            content = msg.get("content")
            if isinstance(content, str):
                return upd, [Event("assistant", **common, role="assistant", model=model, text=content)]
            evs = []
            for p in content or []:
                if not isinstance(p, dict):
                    continue
                pt = p.get("type")
                if pt == "text":
                    evs.append(Event("assistant", **common, role="assistant", model=model,
                                     text=p.get("text") or ""))
                elif pt == "thinking":
                    evs.append(Event("thinking", **common, model=model, text=p.get("thinking") or ""))
                elif pt == "tool_use":
                    args = p.get("input")
                    evs.append(Event("tool_call", **common, model=model, tool_name=p.get("name"),
                                     tool_call_id=p.get("id"), detail=detail_of(args),
                                     text=json.dumps(args, ensure_ascii=False)))
            return upd, evs

        return upd, [Event("other", **common)]


# --------------------------------------------------------------------------- #
# pi-family (pi, OMP). <root> = the agent dir (~/.pi/agent or ~/.omp/agent)
#   sessions/<enc-cwd>/<ts>_<uuid>.jsonl
#   sessions/<enc-cwd>/<ts>_<uuid>/<AgentName>.jsonl          (OMP subagents)
#   sessions/<enc-cwd>/<ts>_<uuid>/__advisor.<name>.jsonl     (OMP advisors)
#   archive/sessions/<enc-cwd>/<file>.jsonl.gz                (archived, same key)
# Format: header {"type":"session",...}, then tree entries linked by id/parentId.
# --------------------------------------------------------------------------- #
class PiAdapter(Adapter):
    name = "pi"

    def classify(self, rel: str) -> KeyInfo | None:
        r = rel[len("archive/"):] if rel.startswith("archive/") else rel
        if not r.startswith("sessions/"):
            return None
        r = r[len("sessions/"):]
        if r.endswith(".gz"):
            r = r[:-3]
        if not r.endswith(".jsonl"):
            return None
        key = r[: -len(".jsonl")]
        parts = key.split("/")
        if len(parts) >= 3:
            name = parts[-1]
            if name.startswith("__advisor."):
                name = "advisor:" + name[len("__advisor."):]
            return KeyInfo(key, True, "/".join(parts[:-1]), name)
        return KeyInfo(key)

    def normalize(self, obj: dict, ctx: dict) -> tuple[dict, list[Event]]:
        t = obj.get("type")
        upd: dict = {}
        if isinstance(obj.get("timestamp"), str):
            upd["ts"] = obj["timestamp"]
        common = dict(entry_id=obj.get("id"), parent_entry_id=obj.get("parentId"),
                      ts=obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None,
                      entry_type=t)

        if t == "session":                       # header, not part of the tree
            upd.update(native_id=obj.get("id"), cwd=obj.get("cwd"),
                       forked_from=obj.get("parentSession"))
            return upd, []
        if t == "session_info":
            upd["title"] = obj.get("name")
            return upd, [Event("meta", **common, text=obj.get("name") or "")]
        if t == "model_change":
            ctx["model"] = obj.get("modelId")
            upd["model"] = obj.get("modelId")
            return upd, [Event("model_change", **common, model=obj.get("modelId"),
                               text=f"{obj.get('provider')}/{obj.get('modelId')}")]
        if t == "compaction":
            return upd, [Event("compaction", **common, text=obj.get("summary") or "")]
        if t == "branch_summary":
            return upd, [Event("branch_summary", **common, text=obj.get("summary") or "")]
        if t == "custom_message":
            return upd, [Event("system", **common, text=text_of(obj.get("content")))]
        if t in ("thinking_level_change", "custom", "label"):
            return upd, [Event("meta", **common)]
        if t != "message":
            return upd, [Event("other", **common)]

        m = obj.get("message") or {}
        role = m.get("role")
        if role == "user":
            return upd, [Event("user", **common, role="user", text=text_of(m.get("content")))]
        if role == "assistant":
            model = m.get("model") or ctx.get("model")
            if m.get("model"):
                upd["model"] = m["model"]
            content = m.get("content")
            if isinstance(content, str):
                return upd, [Event("assistant", **common, role="assistant", model=model, text=content)]
            evs = []
            for p in content or []:
                if not isinstance(p, dict):
                    continue
                pt = p.get("type")
                if pt == "text":
                    evs.append(Event("assistant", **common, role="assistant", model=model,
                                     text=p.get("text") or ""))
                elif pt == "thinking":
                    evs.append(Event("thinking", **common, model=model, text=p.get("thinking") or ""))
                elif pt == "toolCall":
                    args = p.get("arguments")
                    evs.append(Event("tool_call", **common, model=model, tool_name=p.get("name"),
                                     tool_call_id=p.get("id"), detail=detail_of(args),
                                     text=json.dumps(args, ensure_ascii=False)))
            if m.get("stopReason") == "error" and m.get("errorMessage"):
                evs.append(Event("system", **common, model=model, is_error=1,
                                 text=f"[provider error] {m['errorMessage']}"))
            return upd, evs
        if role == "toolResult":
            return upd, [Event("tool_result", **common, tool_name=m.get("toolName"),
                               tool_call_id=m.get("toolCallId"), is_error=int(bool(m.get("isError"))),
                               text=text_of(m.get("content")))]
        if role == "bashExecution":
            code = m.get("exitCode")
            return upd, [Event("user_shell", **common, role="user",
                               is_error=int(code not in (0, None)),
                               detail=(m.get("command") or "")[:200],
                               text=f"$ {m.get('command')}\n{m.get('output') or ''}")]
        return upd, [Event("other", **common, role=role)]


class OmpAdapter(PiAdapter):
    """OMP (oh-my-pi) writes pi's session format with extra subagent/advisor files."""
    name = "omp"


REGISTRY: dict[str, Adapter] = {a.name: a for a in (ClaudeCodeAdapter(), PiAdapter(), OmpAdapter())}
