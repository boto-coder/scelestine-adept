"""User-correction capture for scelestine-adept.

The failure hook only ever sees tool errors. But the most valuable lessons are
the corrections a user gives in chat — "no, use the patch tool, not sed" —
and those never reach a tool call, so nothing recorded them.

Design, mirroring the failure path:
  1. a cheap regex pre-filter decides whether the message *might* be a
     correction. Most turns match nothing and cost nothing: no Jev call, no
     write.
  2. the Jev gate then decides whether it is a reusable lesson, so a one-off
     instruction ("no need for the brass tag") is dropped.

Detection is deliberately generous and the gate is the real filter. A missed
correction is lost learning; a false positive only costs one Jev call.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Optional

from . import decision, store

logger = logging.getLogger("plugins.scelestine-adept.corrections")

# Strong correction signals. Kept narrow on purpose: each one is a phrase that
# almost only appears when the user is correcting behaviour, not describing a
# task. `wrong` alone is NOT here — "the wrong turn" is not a correction.
CORRECTION_RE = re.compile(
    r"(?:"
    r"^\s*(?:no|nope)[,.\s!]"                       # "no, ..." / "no. ..."
    r"|\b(?:don'?t|do\s+not|never)\s+(?:ever\s+)?"
    r"(?:do|use|add|write|say|call|make|run|try|change|edit|touch|mention)\b"
    r"|\bthat'?s\s+(?:wrong|incorrect|not\s+right|not\s+what)\b"
    r"|\bi\s+said\b"
    r"|\b(?:use|do)\s+[^.]{1,60}\s+instead\b"
    r"|\binstead\s+of\b"
    r"|\bstop\s+(?:doing|using|adding|writing|calling|making)\b"
    r"|\bwhy\s+(?:did|are|would)\s+you\b"
    r"|\byou\s+(?:should|must)\s+not\b"
    r"|\bplease\s+don'?t\b"
    r"|\bnot\s+\w+[,.]?\s+(?:but|use|do|just)\b"
    r"|\bno\s+need\s+to\b"
    r")",
    re.IGNORECASE,
)

# Platforms where a "user message" is not a person correcting anything.
DEFAULT_SKIP = ("cron", "background", "subagent")

MAX_LESSON_CHARS = 240

# Gate floor for "is this a reusable lesson?". Measured on this system, two
# independent runs of 3 samples each over 6 genuine corrections and 6 one-off
# remarks (the Jev `noul` answer for one text varies by at most 0.04 between
# samples, so these figures are stable):
#
#   genuine corrections  mean 0.524   range 0.33 - 0.81
#   one-off remarks      mean 0.288   range 0.20 - 0.42
#
#   floor    lessons kept (run 1 / run 2)   remarks dropped
#   0.60     6/18  /  6/18                  18/18   <- the auto_record default
#   0.40    11/18  / 12/18                  17/18
#   0.35    16/18  / 15/18                  15/18
#   0.30    18/18  / 18/18                  10/18
#
# The auto_record default of 0.60 threw away two thirds of genuine corrections
# and caught no more remarks than 0.45 did. 0.35 keeps ~85% of real corrections
# while still rejecting ~85% of one-off remarks, and it held across both runs.
# The failure path keeps its own higher 0.60: a tool error is boilerplate, so
# the bar there should be stricter.
DEFAULT_FLOOR = 0.35


def looks_like_correction(text: str) -> bool:
    """Cheap pre-filter. True means 'worth asking Jev about', not 'is one'."""
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(CORRECTION_RE.search(text))


def candidate(text: str) -> str:
    """The lesson text stored for a correction."""
    body = " ".join(str(text or "").split())[:MAX_LESSON_CHARS]
    return f"User correction: {body}"


def _digest(text: str) -> str:
    return hashlib.sha256(
        f"correction|{text}".encode("utf-8")).hexdigest()[:16]


def maybe_capture(ctx: Any, text: str, *, seen: Optional[set] = None,
                  floor: float = DEFAULT_FLOOR, backend: str = "auto",
                  dedupe: bool = True) -> Optional[str]:
    """Record a user correction as a lesson. Returns its id, or None.

    Never raises: a hook must not break the turn it is observing.
    """
    if ctx is None or not looks_like_correction(text):
        return None

    lesson = candidate(text)
    if not lesson:
        return None

    if dedupe and seen is not None:
        key = _digest(lesson)
        if key in seen:
            return None
        if len(seen) > 512:
            seen.clear()
        seen.add(key)

    try:
        worth = decision.noul(
            state=lesson,
            instructions=("This is a reusable lesson about how to work that "
                          "would help future sessions"),
            backend=backend,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("correction gate failed: %s", type(exc).__name__)
        return None
    if worth is None or worth < floor:
        logger.debug("correction not kept (gate %.3f < %.2f)", worth or 0.0, floor)
        return None

    memory_id, status = store.remember_ex(
        ctx, lesson, importance=0.55, source="user_correction",
        metadata={"kind": "correction"},
    )
    if status == store.STATUS_SAVED:
        logger.info("recorded user correction: %s", memory_id)
        return memory_id
    if status == store.STATUS_ABSENT:
        logger.debug("user correction not stored (no backend)")
    else:
        logger.warning("user correction was NOT stored (write error)")
    return None
