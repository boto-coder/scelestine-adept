"""Recall orchestration for scelestine-adept.

Two stages, both optional-friendly:

  1. Mnemosyne hybrid retrieval (vector + FTS5) builds a shortlist.
  2. One Jev System One call re-ranks that shortlist.

Stage 2 is skipped whenever it adds nothing: fewer than two candidates, or no
credential available. Every failure returns an empty or unranked result so a
hook can simply inject nothing and the turn proceeds unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import decision, store

logger = logging.getLogger("plugins.scelestine-adept.recall")

DEFAULT_SHORTLIST = 12
DEFAULT_TAKE = 3

# Cut on LIFT (score * shortlist size), not the raw score. A `choice` question
# returns a probability distribution summing to 1.0, so a raw score shrinks as
# the shortlist grows: the same correct lesson scored 0.80 with 5 candidates
# and 0.28 with 12. Measured on real queries: the correct lesson scored lift
# 3.36, 4.00 and 1.50, while noise sat at 0.10-1.08. 1.2 keeps every true
# match and drops the noise.
DEFAULT_MIN_LIFT = 1.2

POSITIVE = "A lesson applies when it would change how the task is done now. "

# Added to the injected block so the model knows why it is seeing this.
BLOCK_HEADER = "LESSONS FROM PAST WORK (recalled by scelestine-adept):"
BLOCK_FOOTER = ("Apply only the ones that fit this turn. "
                "If none apply, ignore this block.")


def shortlist(ctx: Any, query: str, limit: int = DEFAULT_SHORTLIST,
              temporal_weight: float = 0.0) -> List[Dict[str, Any]]:
    """Stage 1. Returns [] on any failure."""
    try:
        rows = store.recall(ctx, query, limit=limit,
                            temporal_weight=temporal_weight)
    except Exception as exc:
        logger.debug("shortlist failed: %s", type(exc).__name__)
        return []
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for row in rows:
        key = row.get("id") or row.get("content")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def ranked(ctx: Any, query: str, limit: int = DEFAULT_TAKE,
           shortlist_size: int = DEFAULT_SHORTLIST,
           min_lift: float = DEFAULT_MIN_LIFT,
           temporal_weight: float = 0.0) -> List[Dict[str, Any]]:
    """Stage 1 + 2. Returns the top `limit` rows above `min_lift`, best first.

    The cut is on LIFT (score * shortlist size), not the raw score. A `choice`
    question returns a probability distribution summing to 1.0, so a raw score
    shrinks as the shortlist grows: the SAME correct lesson scored 0.80 with 5
    candidates and 0.28 with 12. An absolute floor therefore discards good
    matches on longer shortlists — measured: "tool failure retry" shortlisted
    12 rows and the old 0.55 floor injected none of them. Lift is scale-free:
    1.0 is uniform, higher means the model actively preferred it.
    """
    rows = shortlist(ctx, query, limit=shortlist_size,
                     temporal_weight=temporal_weight)
    if not rows:
        return []

    # Stage 2 needs at least two candidates to separate.
    if len(rows) < 2:
        return rows[:limit]

    options = {
        (row.get("id") or f"i{i}"): row.get("content", "")
        for i, row in enumerate(rows)
    }
    try:
        scores = decision.rank(
            state=f"Current task: {query}",
            options=options,
            instruction=f"Which of these lessons applies to this task: {query}",
            rubric=POSITIVE,
        )
    except Exception as exc:
        logger.debug("re-rank failed (fail-open, unranked): %s", type(exc).__name__)
        scores = []

    if not scores:
        # No credential, timeout, or bad response — hand back stage 1 as-is.
        return rows[:limit]

    roster = max(1, len(options))
    by_id = {row.get("id"): row for row in rows}
    out: List[Dict[str, Any]] = []
    for key, score in scores:
        value = float(score)
        if value * roster < min_lift:
            continue
        row = by_id.get(key)
        if row is None:
            continue
        enriched = dict(row)
        enriched["score"] = round(value, 4)
        enriched["lift"] = round(value * roster, 3)
        out.append(enriched)
        if len(out) >= limit:
            break

    logger.debug("recall: %d shortlisted -> %d above lift %.2f",
                 len(rows), len(out), min_lift)
    return out


def format_block(rows: List[Dict[str, Any]]) -> str:
    """Render ranked lessons for injection into the user message."""
    if not rows:
        return ""
    lines = [BLOCK_HEADER]
    for row in rows:
        text = " ".join(str(row.get("content", "")).split())
        if text:
            lines.append(f"- {text}")
    lines.append(BLOCK_FOOTER)
    return "\n".join(lines)


def message_text(user_message: Any) -> str:
    """pre_llm_call gives a str for text turns and a parts list for multimodal ones."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = []
        for part in user_message:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""
