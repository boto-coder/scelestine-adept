"""Tool schemas for scelestine-adept.

Pure data. This module imports nothing from its siblings so a schema-only
import can never drag in the network or storage layers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _schema(name: str, description: str, properties: Dict[str, Any],
            required: Optional[List[str]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


def _str(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


def _int(description: str, default: int) -> Dict[str, Any]:
    return {"type": "integer", "description": description, "default": default}


REMEMBER_SCHEMA = _schema(
    "adept_remember",
    "Record one reusable lesson learned from this session. Use it after a mistake, a "
    "correction from the user, a repeated fix, or anything that would help a future "
    "session avoid the same work. Do NOT record one-off facts, task status, or "
    "conversation detail — those belong in normal memory. Storage is Mnemosyne; a "
    "duplicate is detected and reported instead of saved twice.",
    {
        "lesson": _str("One specific, reusable lesson. One sentence. State the trigger "
                       "and the correct action, not the incident narrative. Required."),
        "why": _str("Optional one-line context: what happened that made this a lesson."),
        "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0,
                       "description": "Optional 0..1. Omit to let a Jev System One call "
                                      "estimate how often this will recur."},
    },
    required=["lesson"],
)

RECALL_SCHEMA = _schema(
    "adept_recall",
    "Retrieve past lessons relevant to a task, ranked by relevance. Mnemosyne does "
    "hybrid retrieval first, then a Jev System One call re-ranks the shortlist. Call "
    "this before starting unfamiliar work instead of rediscovering a lesson the hard way.",
    {
        "task": _str("The task, question, or goal to find lessons for. Required."),
        "limit": _int("How many lessons to return. Default 3.", 3),
    },
    required=["task"],
)

REFLECT_SCHEMA = _schema(
    "adept_reflect",
    "Distill one standing behavior from lessons already recorded. A standing behavior "
    "is a durable rule that stays in effect every session, so keep it short, "
    "imperative, and general. Pass an existing memory id to promote it into the "
    "long-term persona store. The rule passes a safety gate before it is written; a "
    "rejected rule is reported with the reason.",
    {
        "behavior": _str("The standing behavior, imperative voice, one sentence, "
                         "under 300 characters. Required."),
        "from_memory_id": _str("Optional Mnemosyne memory_id of the lesson this "
                               "behavior came from; promotes it into long-term storage."),
        "note": _str("Optional one-line rationale, kept with the record."),
    },
    required=["behavior"],
)

REVIEW_SCHEMA = _schema(
    "adept_review",
    "Nightly review: read recent lessons and promote at most a few of them into "
    "standing behaviors. A lesson earns promotion only when it applies broadly to "
    "future work and has shown repeated value. Returns a report of what was "
    "promoted, kept, and discarded. Entry point for the nightly cron job.",
    {
        "limit": _int("Maximum lessons to promote this run. Default 2.", 2),
        "hours": _int("How far back to look. Default 24.", 24),
        "dry_run": {"type": "boolean", "default": False,
                    "description": "Score and report only. Write nothing when true."},
    },
)

SCHEMAS = {
    "adept_remember": REMEMBER_SCHEMA,
    "adept_recall": RECALL_SCHEMA,
    "adept_reflect": REFLECT_SCHEMA,
    "adept_review": REVIEW_SCHEMA,
}
