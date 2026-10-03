"""Mnemosyne adapter for scelestine-adept.

STORAGE IS MNEMOSYNE ONLY. There is no JSONL file, no local lesson database,
and no side-car state anywhere in this plugin.

Access goes through `ctx.dispatch_tool("mnemosyne_*", ...)` — the supported way
for one plugin to reach another plugin's tools. Provider tools are frequently
session-scoped, so a dispatch from a cron run or a fresh session can legitimately
return not_found for a row another session wrote. Every helper here therefore
returns None on any failure and never raises into a hook.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("plugins.scelestine-adept.store")

# Mnemosyne canonical slot group that holds standing behaviors. One slot per
# rule (`rule-01`, `rule-02`, ...); a new value supersedes the old one.
BEHAVIOR_CATEGORY = "behavior"

# Lessons are global so they recall in any later session, not just this one.
LESSON_SCOPE = "global"


def dispatch(ctx: Any, tool: str, args: Dict[str, Any]) -> Any:
    """Call a mnemosyne tool. Returns parsed JSON, a bare string, or None."""
    if ctx is None or not tool:
        return None
    try:
        raw = ctx.dispatch_tool(tool, args)
    except Exception as exc:
        logger.debug("dispatch %s failed: %s", tool, type(exc).__name__)
        return None
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _rows(payload: Any) -> List[Dict[str, Any]]:
    """Normalise the several shapes mnemosyne returns into a list of dicts."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("results", "memories", "items", "slots", "canonical"):
            if isinstance(payload.get(key), list):
                return [r for r in payload[key] if isinstance(r, dict)]
        # a single record dict
        if any(k in payload for k in ("content", "body", "memory_id", "id")):
            return [payload]
    return []


def _id_of(row: Dict[str, Any]) -> str:
    for key in ("memory_id", "id", "canonical_id", "persona_id", "name"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _content_of(row: Dict[str, Any]) -> str:
    for key in ("content", "body", "text", "value"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


# --- writes ---------------------------------------------------------------


def remember(ctx: Any, content: str, importance: float = 0.5,
             source: str = "lesson", metadata: Optional[Dict[str, Any]] = None
             ) -> Optional[str]:
    """Store one lesson. Returns its memory_id, or None."""
    content = (content or "").strip()
    if not content:
        return None
    args: Dict[str, Any] = {
        "content": content,
        "importance": max(0.0, min(1.0, float(importance))),
        "scope": LESSON_SCOPE,
        "source": source,
        "veracity": "stated",
    }
    if metadata:
        args["metadata"] = {str(k): str(v) for k, v in metadata.items()}
    payload = dispatch(ctx, "mnemosyne_remember", args)
    if isinstance(payload, dict):
        for key in ("memory_id", "id"):
            if isinstance(payload.get(key), str) and payload[key]:
                logger.info("recorded lesson %s", payload[key])
                return payload[key]
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    # A missing id usually means Mnemosyne is unavailable in this context.
    # That is a normal condition for a session-scoped provider, so DEBUG.
    logger.debug("mnemosyne_remember returned no id")
    return None


def recall(ctx: Any, query: str, limit: int = 12,
           temporal_weight: float = 0.0) -> List[Dict[str, Any]]:
    """Hybrid (vector + FTS5) recall. Empty list on any failure."""
    if not query or not query.strip():
        return []
    args: Dict[str, Any] = {"query": query.strip(), "limit": int(limit)}
    if temporal_weight:
        args["temporal_weight"] = float(temporal_weight)
    payload = dispatch(ctx, "mnemosyne_recall", args)
    rows = _rows(payload)
    out: List[Dict[str, Any]] = []
    for row in rows:
        text = _content_of(row)
        if text:
            out.append({"id": _id_of(row), "content": text, "row": row})
    return out


def canonical_put(ctx: Any, name: str, body: str,
                  category: str = BEHAVIOR_CATEGORY) -> bool:
    """Write one canonical behavior slot. A new value supersedes the old one."""
    body = (body or "").strip()
    if not name or not body:
        return False
    payload = dispatch(ctx, "mnemosyne_remember_canonical",
                       {"category": category, "name": name, "body": body})
    return payload is not None


def canonical_list(ctx: Any, category: str = BEHAVIOR_CATEGORY
                   ) -> List[Dict[str, Any]]:
    """Read every current slot in a canonical group."""
    payload = dispatch(ctx, "mnemosyne_recall_canonical", {"category": category})
    out: List[Dict[str, Any]] = []
    for row in _rows(payload):
        name = str(row.get("name") or "").strip()
        body = _content_of(row)
        if name and body:
            out.append({"name": name, "content": body})
    return out


def canonical_retire(ctx: Any, name: str,
                     category: str = BEHAVIOR_CATEGORY) -> bool:
    """Stamp a slot as history. Nothing is deleted."""
    payload = dispatch(ctx, "mnemosyne_forget_canonical",
                       {"category": category, "name": name})
    return payload is not None


def persona_promote(ctx: Any, memory_id: str, reason: str = "") -> bool:
    """Move a lesson into the L3 persona store so it is reinforced over time."""
    if not memory_id:
        return False
    args: Dict[str, Any] = {"memory_id": memory_id, "tier": "long_term"}
    if reason:
        args["reason"] = reason[:400]
    return dispatch(ctx, "mnemosyne_persona_promote", args) is not None


def graph_link(ctx: Any, source_id: str, target_id: str,
               relationship: str = "derived_from") -> bool:
    """Connect a promoted behavior back to the lesson it came from."""
    if not source_id or not target_id:
        return False
    args = {"source_id": source_id, "target_id": target_id,
            "relationship": relationship, "weight": 0.6}
    return dispatch(ctx, "mnemosyne_graph_link", args) is not None
