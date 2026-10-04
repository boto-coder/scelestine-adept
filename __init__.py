"""scelestine-adept — self-learning for Scelestine.

Records lessons, recalls and ranks them for the current task, and distills
standing behaviors into SOUL.md.

Storage: Mnemosyne only. No JSONL, no local lesson file.
Re-ranking: one Jev System One call per recall, fail-open.
Surface: 4 tools, 2 hooks (pre_llm_call injects + records corrections,
post_tool_call auto-records failures).

What it learns from, automatically:
  * a tool failure (post_tool_call)   -> "retry differently" lesson
  * a user correction (pre_llm_call)  -> "User correction: ..." lesson
Both pass a Jev gate first, so a one-off remark is not stored as a lesson.
Non-failure, non-correction lessons can still be stored on demand through
the adept_remember and adept_reflect tools.

register() performs registrations only — no network calls, no file writes.
Every hook body is wrapped so an exception is logged and the turn continues.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from . import (corrections, decision, identity, recall, review, schemas,
               store)

logger = logging.getLogger("plugins.scelestine-adept")

TOOLSET = "scelestine_adept"
OWN_TOOLS = {"adept_remember", "adept_recall", "adept_reflect", "adept_review"}
SKIP_PREFIXES = ("mnemosyne_", "adept_")

DEFAULTS: Dict[str, Any] = {
    "backend": "auto",              # auto | typesafe | local
    "skip_platforms": [],           # e.g. ["telegram"]
    "min_message_chars": 20,        # ignore greetings for injection
    "inject_top": 3,                # lessons injected per turn
    "inject_min_lift": 1.2,         # Jev lift (score * shortlist) below this is dropped
    "auto_record": True,            # post_tool_call writes a lesson on failure
    "auto_record_floor": 0.60,      # Jev gate for whether a failure is a lesson
    "learn_corrections": True,      # pre_llm_call records user corrections
    "correction_floor": 0.35,       # Jev gate: see corrections.DEFAULT_FLOOR
    "jev_safety_gate": True,        # second-layer rule gate in adept_reflect
    "shortlist": 12,                # Mnemosyne candidates before re-ranking
}

# Set once by register(); hooks reach Mnemosyne and SOUL.md through it.
_CTX: Any = None

# Failures already considered this process, so one broken tool cannot spam
# identical lessons. Bounded, in-memory, deliberately lost on restart.
_SEEN: set = set()


def _cfg(ctx: Any, key: str, default: Any = None) -> Any:
    """Read plugin config, falling back to DEFAULTS."""
    if default is None:
        default = DEFAULTS.get(key)
    if ctx is None:
        return default
    try:
        value = ctx.get_config(key, None)
    except Exception:
        return default
    return default if value is None else value


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    body = {"error": message}
    body.update(extra)
    return json.dumps(body, ensure_ascii=False)


# --- tool: adept_remember -------------------------------------------------


def _handle_remember(args: Dict[str, Any], **kwargs: Any) -> str:
    lesson = str(args.get("lesson") or "").strip()
    if not lesson:
        return _err("lesson is required")
    if len(lesson) > 600:
        return _err("lesson too long", max_chars=600)
    if _CTX is None:
        return _err("plugin context unavailable")

    ctx = _CTX
    floor = float(_cfg(ctx, "auto_record_floor", 0.60))
    backend = _cfg(ctx, "backend", "auto")

    # Duplicate check: is this already recorded?
    existing = recall.shortlist(ctx, lesson, limit=6)
    if existing:
        state = ("NEW LESSON:\n" + lesson + "\n\nALREADY RECORDED:\n"
                 + "\n".join(f"- {r.get('content', '')[:200]}" for r in existing))
        dup = decision.noul(
            state=state,
            instructions="The new lesson says the same thing as one of the already recorded lessons",
            backend=backend,
        )
        if dup is not None and dup >= 0.75:
            match = existing[0]
            return _ok({
                "saved": False,
                "duplicate": True,
                "match_score": round(dup, 3),
                "existing_id": match.get("id", ""),
                "existing_content": match.get("content", "")[:200],
            })

    # Importance: caller-supplied, otherwise estimated.
    importance = args.get("importance")
    if isinstance(importance, (int, float)) and not isinstance(importance, bool):
        importance = max(0.0, min(1.0, float(importance)))
        source = "given"
    else:
        recurring = decision.noul(
            state=lesson,
            instructions="This mistake or correction will recur in future sessions",
            backend=backend,
        )
        importance = round(0.3 + 0.6 * (recurring if recurring is not None else 0.4), 3)
        source = "estimated"

    why = str(args.get("why") or "").strip()
    content = f"{lesson}\nContext: {why}" if why else lesson
    memory_id, status = store.remember_ex(
        ctx, content, importance=importance,
        metadata={"source_tool": "adept_remember"},
    )
    if not memory_id:
        if status == store.STATUS_ABSENT:
            return _err(
                "Mnemosyne backend not loaded in this context and the "
                "in-process fallback is unavailable; lesson NOT stored",
                lesson=lesson[:120], status=status,
            )
        return _err("Mnemosyne write failed; lesson not stored",
                    lesson=lesson[:120], status=status)

    return _ok({
        "saved": True,
        "memory_id": memory_id,
        "importance": importance,
        "importance_source": source,
        "candidates_checked": len(existing),
    })


# --- tool: adept_recall ---------------------------------------------------


def _handle_recall(args: Dict[str, Any], **kwargs: Any) -> str:
    task = str(args.get("task") or "").strip()
    if not task:
        return _err("task is required")
    if _CTX is None:
        return _err("plugin context unavailable")

    ctx = _CTX
    try:
        limit = int(args.get("limit") or recall.DEFAULT_TAKE)
    except (TypeError, ValueError):
        limit = recall.DEFAULT_TAKE
    limit = max(1, min(10, limit))

    rows = recall.ranked(
        ctx, task, limit=limit,
        shortlist_size=int(_cfg(ctx, "shortlist", 12)),
        min_lift=float(_cfg(ctx, "inject_min_lift", 1.2)),
    )
    return _ok({
        "task": task,
        "count": len(rows),
        "lessons": [
            {"memory_id": r.get("id", ""), "score": r.get("score"),
             "lift": r.get("lift"),
             "content": r.get("content", "")[:400]}
            for r in rows
        ],
        "note": "ranked by Mnemosyne retrieval + Jev System One re-rank; "
                "empty means no lesson cleared the lift floor",
    })


# --- tool: adept_reflect --------------------------------------------------


def _handle_reflect(args: Dict[str, Any], **kwargs: Any) -> str:
    rule = " ".join(str(args.get("behavior") or "").split())
    if not rule:
        return _err("behavior is required")
    if _CTX is None:
        return _err("plugin context unavailable")

    ctx = _CTX
    backend = _cfg(ctx, "backend", "auto")

    allowed, reason = identity.gate(rule)
    if not allowed:
        return _ok({"saved": False, "rejected": True, "reason": reason})

    if bool(_cfg(ctx, "jev_safety_gate", True)):
        ok, score = identity.jev_gate(rule)
        if not ok:
            return _ok({
                "saved": False, "rejected": True,
                "reason": "safety gate: rule smuggles an instruction",
                "jev_safety": round(score, 3),
            })
        jev_safety = round(score, 3)
    else:
        jev_safety = None

    rows = identity.behaviors(ctx)
    dup = identity.duplicate_of(rows, rule)
    if dup:
        return _ok({"saved": False, "duplicate": True, "existing_slot": dup})

    if len(rows) >= identity.MAX_BEHAVIORS:
        oldest = rows[0]
        if not store.canonical_retire(ctx, str(oldest.get("name", ""))):
            return _err("at capacity and could not retire the oldest slot",
                        capacity=identity.MAX_BEHAVIORS)

    slot = identity.next_slot(identity.behaviors(ctx))
    if not store.canonical_put(ctx, slot, rule):
        return _err("Mnemosyne canonical write failed")

    note = str(args.get("note") or "").strip()
    if note:
        store.remember(ctx, f"{rule}\nRationale: {note}", importance=0.7,
                       source="behavior")

    from_id = str(args.get("from_memory_id") or "")
    promoted = False
    if from_id:
        promoted = store.persona_promote(ctx, from_id, reason=rule)
        store.graph_link(ctx, from_id, slot, relationship="promoted_to")

    projected = identity.project(ctx)
    return _ok({
        "saved": True,
        "slot": slot,
        "soul_md_behaviors": projected,
        "promoted_to_persona": promoted,
        "jev_safety": jev_safety,
        "capacity": identity.MAX_BEHAVIORS,
    })


# --- tool: adept_review ---------------------------------------------------


def _handle_review(args: Dict[str, Any], **kwargs: Any) -> str:
    if _CTX is None:
        return _err("plugin context unavailable")
    try:
        limit = int(args.get("limit", 2))
    except (TypeError, ValueError):
        limit = 2
    try:
        hours = int(args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    report = review.run(_CTX, limit=limit, hours=hours,
                        dry_run=bool(args.get("dry_run", False)))
    if report.get("error"):
        return _ok(report)
    return _ok(report)


# --- hooks ----------------------------------------------------------------


def _on_turn(session_id: str, user_message: Any, conversation_history: list,
             is_first_turn: bool, model: str, platform: str,
             **kwargs: Any) -> Optional[Dict[str, str]]:
    """pre_llm_call: inject the lessons that fit this turn. Fail-open by design."""
    ctx = _CTX
    if ctx is None:
        return None
    try:
        skip = _cfg(ctx, "skip_platforms", []) or []
        if platform in skip:
            return None

        text = recall.message_text(user_message).strip()
        if len(text) < int(_cfg(ctx, "min_message_chars", 20)):
            return None
        if text.startswith("/"):          # slash commands are not tasks
            return None

        # A correction the user just gave is worth recording. Runs BEFORE the
        # injection path and independently of it, so a correction is still
        # learned on turns where no lesson was injected. Fail-open, and the
        # regex pre-filter keeps it cheap: most turns cost no Jev call.
        if bool(_cfg(ctx, "learn_corrections", True)):
            try:
                corrections.maybe_capture(
                    ctx, text, seen=_SEEN,
                    floor=float(_cfg(ctx, "correction_floor",
                                     corrections.DEFAULT_FLOOR)),
                    backend=_cfg(ctx, "backend", "auto"),
                )
            except Exception as exc:
                logger.debug("correction capture failed (fail-open): %s",
                             type(exc).__name__)

        rows = recall.ranked(
            ctx, text,
            limit=int(_cfg(ctx, "inject_top", 3)),
            shortlist_size=int(_cfg(ctx, "shortlist", 12)),
            min_lift=float(_cfg(ctx, "inject_min_lift", 1.2)),
        )
        if not rows:
            return None

        block = recall.format_block(rows)
        logger.info("injecting %d lesson(s) on %s", len(rows), platform)
        return {"context": block}
    except Exception as exc:
        logger.warning("pre_llm_call failed (fail-open): %s", type(exc).__name__)
        return None


def _failure(status: Any, error_type: Any, error_message: Any,
             result: Any) -> bool:
    """Best-effort failure detection across the values the hook contract ships."""
    if error_type:
        return True
    if isinstance(status, str) and status.lower() in {
            "error", "failed", "failure", "timeout", "timed_out"}:
        return True
    if isinstance(error_message, str) and error_message.strip():
        return True
    if isinstance(result, str) and result:
        head = result[:2000]
        if '"error"' in head and '"error": null' not in head and '"error":""' not in head:
            return True
        if head.lstrip().startswith("Error:") or head.lstrip().startswith("ERROR"):
            return True
        if "Traceback (most recent call last)" in head:
            return True
    return False


_BACKEND_ABSENT_WARNED = False


def _warn_backend_absent(where: str) -> None:
    """One loud line per process. An absent backend must never look like a success."""
    global _BACKEND_ABSENT_WARNED
    if _BACKEND_ABSENT_WARNED:
        logger.debug("mnemosyne backend absent (already reported) in %s", where)
        return
    _BACKEND_ABSENT_WARNED = True
    logger.warning(
        "mnemosyne storage unavailable in this context: no provider and the "
        "in-process fallback is unavailable too (skip_memory fork, e.g. "
        "background_review or curator). Auto-record skipped, lesson NOT "
        "stored. Where: %s", where,
    )


def _handle_failed_tool(tool_name: str, error_text: str) -> None:
    """auto_record: decide whether this failure is worth a lesson, then store it."""
    ctx = _CTX
    if ctx is None:
        return
    if not bool(_cfg(ctx, "auto_record", True)):
        return
    if any(tool_name.startswith(p) for p in SKIP_PREFIXES):
        return

    # Prove a write path exists BEFORE spending a SystemOne call on a lesson
    # that cannot be stored. Lazy probe, never at plugin load: PluginManager
    # runs register() before _init_memory binds the provider.
    if store.write_path(ctx) == "none":
        _warn_backend_absent(f"post_tool_call/{tool_name}")
        return

    digest = hashlib.sha256(f"{tool_name}|{error_text}".encode()).hexdigest()[:16]
    if digest in _SEEN:
        return
    if len(_SEEN) > 512:
        _SEEN.clear()
    _SEEN.add(digest)

    candidate = (f"After a {tool_name} failure, retry differently: "
                 + " ".join(error_text.split())[:240])
    try:
        worth = decision.noul(
            state=candidate,
            instructions=("This is a reusable lesson that would help future "
                          "sessions avoid the same failure"),
            backend=_cfg(ctx, "backend", "auto"),
        )
    except Exception as exc:
        logger.debug("auto_record gate failed: %s", type(exc).__name__)
        worth = None
    if worth is None or worth < float(_cfg(ctx, "auto_record_floor", 0.60)):
        return

    memory_id, status = store.remember_ex(
        ctx, candidate, importance=0.45, source="auto_record",
        metadata={"tool": tool_name},
    )
    if status == store.STATUS_SAVED:
        logger.info("auto-recorded lesson from %s failure: %s", tool_name, memory_id)
    elif status == store.STATUS_ABSENT:
        # write_path() said a path existed, then it vanished: report it.
        _warn_backend_absent(f"post_tool_call/{tool_name}")
    else:
        logger.warning("auto-recorded lesson from %s failure was NOT stored "
                       "(write error)", tool_name)


def _on_tool(tool_name: str, args: Dict[str, Any], result: Any,
             **kwargs: Any) -> None:
    """post_tool_call: observer only — return value is ignored by the framework."""
    try:
        if not isinstance(tool_name, str) or not tool_name:
            return
        if not _failure(kwargs.get("status"), kwargs.get("error_type"),
                        kwargs.get("error_message"), result):
            return
        error_text = str(kwargs.get("error_message") or result or "")
        if not error_text.strip():
            return
        _handle_failed_tool(tool_name, error_text)
    except Exception as exc:
        logger.debug("post_tool_call failed (fail-open): %s", type(exc).__name__)


# --- registration ---------------------------------------------------------

_TOOLS = (
    ("adept_remember", schemas.REMEMBER_SCHEMA, _handle_remember, "📝"),
    ("adept_recall", schemas.RECALL_SCHEMA, _handle_recall, "🔎"),
    ("adept_reflect", schemas.REFLECT_SCHEMA, _handle_reflect, "🪞"),
    ("adept_review", schemas.REVIEW_SCHEMA, _handle_review, "🌙"),
)


def register(ctx) -> None:
    """Called once by the plugin loader. Registrations only — no side effects."""
    global _CTX
    _CTX = ctx

    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, description=schema["description"],
                          emoji=emoji)
    ctx.register_hook("pre_llm_call", _on_turn)
    ctx.register_hook("post_tool_call", _on_tool)
    logger.info("scelestine-adept registered 4 tools and 2 hooks")
