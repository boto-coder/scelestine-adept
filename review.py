"""Nightly review for scelestine-adept.

Reads recent lessons, scores them against a "does this earn a standing
behavior?" rubric, promotes the winners into Mnemosyne canonical slots, and
rebuilts the SOUL.md projection.

Promotion policy: at most `limit` lessons per run (default 2), a behavior must
clear both the Jev confidence floor and the safety gate, and the behavior count
is capped — the oldest slot retires when the cap is reached. Nothing is deleted
from Mnemosyne; retiring a slot only stamps it as history.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import decision, identity, recall, store

logger = logging.getLogger("plugins.scelestine-adept.review")

# Kept only as a fallback pool when the direct listing is empty. NOT a good
# primary: an abstract question scores fts=0 against every lesson, so the
# relevance cutoff drops them all (measured: 18 found, 0 kept).
REVIEW_QUERY = "after a tool failure, retry differently"
REVIEW_SHORTLIST = 20

# A `choice` question returns a PROBABILITY DISTRIBUTION that sums to 1.0, so
# its scale depends on roster size: the same lesson scores 0.62 among 3 options
# and 0.25 among 17. An absolute floor is therefore meaningless — 0.60 was
# unreachable for any roster above ~2 and silently promoted nothing.
#
# The scale-free measure is LIFT = probability * roster size. 1.0 is "no better
# than uniform"; higher means the model actively preferred it. Measured on a
# real pool of 7 distinct lessons: the winner scored lift 5.11, the runner-up
# 1.12. A floor of 1.5 keeps the clear winner, drops the noise, and needs no
# magic constant tied to roster size.
REVIEW_MIN_LIFT = 1.5

LEGEND = (
    "Promote only when the lesson applies broadly to future work and has shown "
    "repeated value. Keep it as data when it is specific to one task, still "
    "one-off, or too narrow to guide future behaviour."
)


def score_lessons(lessons: List[Dict[str, Any]],
                  min_lift: float = REVIEW_MIN_LIFT) -> List[Dict[str, Any]]:
    """Stage 1 (Mnemosyne) + stage 2 (Jev), with a promotion rubric.

    Keeps lessons whose lift clears ``min_lift``. ``lift`` is
    ``score * len(options)`` and is stored on each row alongside ``score``.
    """
    if len(lessons) < 2:
        return []
    options = {
        (row.get("id") or f"i{i}"): row.get("content", "")
        for i, row in enumerate(lessons)
    }
    try:
        scores = decision.rank(
            state="Nightly review: pick which of these lessons earns a "
                  "standing behavior.",
            options=options,
            instruction="Which of these lessons should be promoted to a "
                        "standing behavior",
            rubric=LEGEND,
        )
    except Exception as exc:
        logger.debug("review scoring failed: %s", type(exc).__name__)
        scores = []

    roster = max(1, len(options))
    by_id = {row.get("id"): row for row in lessons}
    out: List[Dict[str, Any]] = []
    for key, score in scores:
        row = by_id.get(key)
        if row is None:
            continue
        value = float(score)
        enriched = dict(row)
        enriched["score"] = round(value, 4)
        enriched["lift"] = round(value * roster, 3)
        out.append(enriched)
    out.sort(key=lambda r: -r["lift"])
    return [r for r in out if r["lift"] >= min_lift]


def _rule_from_lesson(text: str) -> str:
    """Turn a lesson into a short imperative rule."""
    rule = " ".join(str(text).split())
    # Drop the storage marker so it never leaks into SOUL.md.
    marker = store.LESSON_MARKER
    if rule.upper().startswith(marker):
        rule = rule[len(marker):].strip()
    if len(rule) > 380:
        rule = rule[:377].rstrip() + "..."
    return rule


def _enforce_cap(ctx: Any, rows: List[Dict[str, str]]) -> List[str]:
    """Retire the oldest slots past the cap. Returns the retired slot names."""
    retired: List[str] = []
    overflow = len(rows) - identity.MAX_BEHAVIORS
    if overflow <= 0:
        return retired
    for row in rows[:overflow]:
        name = str(row.get("name", ""))
        if name and store.canonical_retire(ctx, name):
            retired.append(name)
    return retired


def run(ctx: Any, limit: int = 2, hours: int = 24, dry_run: bool = False
        ) -> Dict[str, Any]:
    """Promote up to `limit` recent lessons into standing behaviors."""
    limit = max(0, min(5, int(limit)))
    report: Dict[str, Any] = {
        "promoted": [],
        "kept": [],
        "rejected": [],
        "retired": [],
        "dry_run": bool(dry_run),
        "promoted_count": 0,
        "error": "",
    }
    if limit == 0:
        report["error"] = "limit is 0; nothing to promote"
        return report

    lessons = store.recent_lessons(ctx, hours=hours, limit=REVIEW_SHORTLIST)
    if not lessons:
        # Direct listing can legitimately be empty (a context that sees no
        # rows). Fall back to a keyword-rich recall query, which does score
        # against lesson text.
        lessons = recall.shortlist(
            ctx, REVIEW_QUERY, limit=REVIEW_SHORTLIST,
            temporal_weight=min(0.9, max(0.1, hours / 24.0)),
        )
    if not lessons:
        report["error"] = "no lessons found (Mnemosyne returned nothing)"
        return report

    candidates = score_lessons(lessons)
    if not candidates:
        report["kept"] = [
            {"id": r.get("id", ""), "content": r.get("content", "")[:200]}
            for r in lessons[:5]
        ]
        report["error"] = "no lesson cleared the promotion floor"
        return report

    existing = identity.behaviors(ctx)
    taken = {str(r.get("name", "")) for r in existing}
    slot = identity.next_slot(existing)

    for row in candidates[:limit]:
        memory_id = str(row.get("id") or "")
        content = str(row.get("content") or "").strip()
        rule = _rule_from_lesson(content)
        entry = {"id": memory_id, "score": row.get("score"),
                 "lift": row.get("lift"),
                 "content": content[:200], "rule": rule}

        allowed, reason = identity.gate(rule)
        if allowed:
            dup = identity.duplicate_of(existing, rule)
            if dup:
                entry["reason"] = f"duplicate of existing slot {dup}"
                report["rejected"].append(entry)
                continue
            jev_ok, jev_score = identity.jev_gate(rule)
            entry["jev_safety"] = round(jev_score, 3)
            if not jev_ok:
                entry["reason"] = "safety gate: rule smuggles an instruction"
                report["rejected"].append(entry)
                continue
        else:
            entry["reason"] = reason
            report["rejected"].append(entry)
            continue

        if dry_run:
            entry["reason"] = "dry run; would promote"
            report["promoted"].append(entry)
            continue

        # Make room before writing.
        rows_now = identity.behaviors(ctx)
        report["retired"].extend(_enforce_cap(ctx, rows_now))

        if slot in taken:
            slot = identity.next_slot(identity.behaviors(ctx))
        if not store.canonical_put(ctx, slot, rule):
            entry["reason"] = "Mnemosyne write failed"
            report["rejected"].append(entry)
            continue
        taken.add(slot)

        if memory_id:
            store.persona_promote(ctx, memory_id, reason=rule)
        report["promoted"].append(entry)
        report["promoted_count"] += 1
        next_num = int(slot.split("-")[1]) + 1 if "-" in slot else 1
        slot = f"rule-{next_num:02d}"

    if not dry_run and report["promoted"]:
        identity.project(ctx)

    logger.info("review: promoted=%d rejected=%d kept=%d",
                report["promoted_count"], len(report["rejected"]),
                len(report["kept"]))
    return report
