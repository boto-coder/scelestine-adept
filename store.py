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

import glob
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("plugins.scelestine-adept.store")

# Mnemosyne canonical slot group that holds standing behaviors. One slot per
# rule (`rule-01`, `rule-02`, ...); a new value supersedes the old one.
BEHAVIOR_CATEGORY = "behavior"

# Lessons are global so they recall in any later session, not just this one.
LESSON_SCOPE = "global"

# Write outcomes. Three-way on purpose: a missing backend is a different
# condition from a failed write, and it must never be reported as success.
STATUS_SAVED = "saved"
STATUS_ABSENT = "backend_absent"
STATUS_FAILED = "write_failed"

# Read-only probe. The provider registers all 41 tools in one loop, so any
# member proves the whole bundle is present.
_PROBE_TOOL = "mnemosyne_stats"

# id(ctx) -> (ctx, verdict). The ctx is held alive in the value so a
# garbage-collected object's id cannot be reused by a different context and
# inherit a stale verdict.
_probe_cache: Dict[int, Any] = {}


def _error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        return str(err) if err else ""
    return ""


def _is_unknown_backend(payload: Any) -> bool:
    """True only for registry.dispatch's `Unknown tool` miss — the skip_memory signature."""
    return "unknown tool" in _error_text(payload).lower()


def available(ctx: Any) -> bool:
    """True when the mnemosyne provider is reachable from this context.

    Probed lazily on first use and cached per context — never at plugin load.
    PluginManager runs register(ctx) before ``_init_memory`` binds the provider,
    so a load-time probe would report "absent" in a healthy session and block
    every later write. ``skip_memory`` forks (background_review, curator) never
    bind the provider at all, so their verdict stays False for the whole process.
    """
    if ctx is None:
        return False
    key = id(ctx)
    hit = _probe_cache.get(key)
    if hit is not None:
        if hit[0] is ctx:
            return bool(hit[1])
    payload = dispatch(ctx, _PROBE_TOOL, {})
    # Only an explicit "Unknown tool" proves absence. Any other error, or a real
    # stats payload, means the provider is loaded — let the write itself decide.
    verdict = not _is_unknown_backend(payload)
    if len(_probe_cache) > 64:
        _probe_cache.clear()
    _probe_cache[key] = (ctx, verdict)
    return verdict


# --- in-process fallback ----------------------------------------------------
# skip_memory forks (background_review, curator) never bind the mnemosyne
# provider, so ctx.dispatch_tool("mnemosyne_remember") returns "Unknown tool".
# The provider is not importable here by default (ModuleNotFoundError,
# verified), so add its venv site-packages to sys.path and call Mnemosyne
# directly. Properties that were verified before choosing this path:
#   * same DB as the provider (both reported 35 working memories at
#     ~/.hermes/mnemosyne/data/mnemosyne.db) and it embeds on write
#     (store -> recall returned the row, score 0.551);
#   * WRITE-ONLY — it never reads, so it does not reopen the session-scoped
#     read leak that skip_memory exists to prevent;
#   * scope passed explicitly as LESSON_SCOPE ("global"), so a lesson written
#     during a fork stays recallable in later sessions (the CLI's implicit
#     "session" default would not, and no env var is needed in-process).
# The path is APPENDED, never prepended: every package Hermes already ships
# keeps its own version and wins over the venv's.
_SP_GLOB = "~/.hermes/installs/*/environments/*/venv/lib/python*/site-packages"
_UNSET = object()

_sp_path: Any = _UNSET       # site-packages dir, None, or unresolved
_im_ready: Any = _UNSET      # (Mnemosyne, db_path, bank), None, or unresolved


def find_packages() -> Optional[str]:
    """site-packages that ship mnemosyne, or None. Resolved once per process."""
    global _sp_path
    if _sp_path is not _UNSET:
        return _sp_path
    _sp_path = None
    for cand in sorted(glob.glob(os.path.expanduser(_SP_GLOB)),
                       key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                       reverse=True):
        if os.path.isdir(os.path.join(cand, "mnemosyne")):
            _sp_path = cand
            break
    if _sp_path:
        logger.debug("mnemosyne fallback site-packages: %s", _sp_path)
    else:
        logger.debug("mnemosyne fallback unavailable (site-packages not found)")
    return _sp_path


def _load_backend() -> Any:
    """Import mnemosyne once and resolve the DB. Returns (Mnemosyne, db_path,
    bank), or None when unavailable. The result — including failure — is cached
    so a broken environment costs one attempt, not one per lesson."""
    global _im_ready
    if _im_ready is not _UNSET:
        return _im_ready
    _im_ready = None
    sp = find_packages()
    if not sp:
        return None
    if sp not in sys.path:
        sys.path.append(sp)
    try:
        from mnemosyne.core.memory import Mnemosyne  # noqa: PLC0415
        from mnemosyne.core.banks import BankManager  # noqa: PLC0415
        # Same resolution order as mnemosyne/cli.py:_default_data_dir.
        data_dir = os.environ.get("MNEMOSYNE_DATA_DIR")
        if not data_dir:
            hermes_home = os.environ.get("HERMES_HOME")
            data_dir = (str(Path(hermes_home).expanduser() / "mnemosyne" / "data")
                        if hermes_home
                        else str(Path.home() / ".hermes" / "mnemosyne" / "data"))
        bank = (os.environ.get("MNEMOSYNE_BANK") or "default").strip()
        db_path = str(BankManager(Path(data_dir)).get_bank_db_path(bank))
    except Exception as exc:  # noqa: BLE001 — never take the gateway down
        logger.warning("mnemosyne fallback unusable: %s", type(exc).__name__)
        return None
    _im_ready = (Mnemosyne, db_path, bank)
    logger.debug("mnemosyne fallback ready: db=%s bank=%s", db_path, bank)
    return _im_ready


def _memory_store(content: str, source: str, importance: float) -> Optional[str]:
    """Write one lesson in-process. Returns its memory_id, or None."""
    ready = _load_backend()
    if not ready:
        return None
    content = (content or "").strip()
    if not content:
        return None
    Mnemosyne, db_path, bank = ready
    try:
        # A fresh handle per write, like one CLI run: no long-lived connection
        # is parked inside the gateway process.
        mem = Mnemosyne(db_path=db_path, bank=bank)
        memory_id = mem.remember(
            content,
            source=source,
            importance=max(0.0, min(1.0, float(importance))),
            scope=LESSON_SCOPE,
            extract_entities=True,
        )
    except Exception as exc:  # noqa: BLE001 — a bad write must not kill the hook
        logger.warning("mnemosyne in-process store failed: %s", type(exc).__name__)
        return None
    if isinstance(memory_id, str) and memory_id:
        logger.info("recorded lesson via mnemosyne fallback: %s", memory_id)
        return memory_id
    logger.debug("mnemosyne in-process store returned no id")
    return None


def write_path(ctx: Any) -> str:
    """Which write path exists here: ``provider`` | ``fallback`` | ``none``."""
    if available(ctx):
        return "provider"
    return "fallback" if _load_backend() else "none"


def remember_ex(ctx: Any, content: str, importance: float = 0.5,
                source: str = "lesson", metadata: Optional[Dict[str, Any]] = None
                ) -> Any:
    """Store one lesson. Returns ``(memory_id_or_None, status)``."""
    if ctx is None:
        # No context means the plugin is not registered here: never write.
        return None, STATUS_FAILED
    content = (content or "").strip()
    if not content:
        return None, STATUS_FAILED
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
    provider_status = STATUS_FAILED
    if isinstance(payload, dict):
        if _is_unknown_backend(payload):
            provider_status = STATUS_ABSENT
        else:
            for key in ("memory_id", "id"):
                val = payload.get(key)
                if isinstance(val, str) and val:
                    logger.info("recorded lesson %s", val)
                    return val, STATUS_SAVED
            err = _error_text(payload)
            if err:
                logger.warning("mnemosyne_remember error: %s", err[:200])
            provider_status = STATUS_FAILED
    elif isinstance(payload, str) and payload.strip():
        return payload.strip(), STATUS_SAVED
    else:
        logger.debug("mnemosyne_remember returned no usable payload")
        provider_status = STATUS_FAILED

    # Only "backend absent" (the skip_memory fork) falls back. A provider
    # exception means the store is reachable but unhappy — keep the original
    # contract and surface it instead of silently writing elsewhere.
    if provider_status == STATUS_ABSENT:
        fallback_id = _memory_store(content, source,
                                    max(0.0, min(1.0, float(importance))))
        if fallback_id:
            return fallback_id, STATUS_SAVED
    return None, provider_status


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
    """Store one lesson. Returns its memory_id, or None.

    Kept for existing callers. Use :func:`remember_ex` when the caller must tell
    ``backend_absent`` from ``write_failed``.
    """
    return remember_ex(ctx, content, importance=importance, source=source,
                       metadata=metadata)[0]


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
