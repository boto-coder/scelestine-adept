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

REVIEW_QUERY = ("recurring lesson from past work: a mistake that repeated, a "
                "correction the user gave more than once, or a fix that kept "
                "being needed")
REVIEW_SHORTLIST = 20
REVIEW_FLOOR = 0.60

LEGEND = (
    "Promote only when the lesson applies broadly to future work and has shown "
    "repeated value. Keep it as data when it is specific to one task, still "
    "one-off, or too narrow to guide future behaviour."
)


def score_lessons(lessons: List[Dict[str, Any]], floor: float = REVIEW_FLOOR
                  ) -> List[Dict[str, Any]]:
    """Stage 1 (Mnemosyne) + stage 2 (Jev), with a promotion rubric."""
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

    by_id = {row.get("id"): row for row in lessons}
    out: List[Dict[str, Any]] = []
    for key, score in scores:
        row = by_id.get(key)
        if row is None:
            continue
        enriched = dict(row)
        enriched["score"] = round(float(score), 4)
        out.append(enriched)
    out.sort(key=lambda r: -r["score"])
    return [r for r in out if r["score"] >= floor]


def _rule_from_lesson(text: str) -> str:
    """Turn a lesson into a short imperative rule."""
    rule = " ".join(str(text).split())
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
