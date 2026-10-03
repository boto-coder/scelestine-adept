"""Jev System One client for scelestine-adept.

Two backends, tried in order, both fail-open (any failure returns None and the
caller proceeds unranked):

  1. TypeSafe direct  POST https://api.typesafe.ai/v1/systemone   model jev-latest
  2. Local router     POST http://localhost:20128/v1/systemone     model oc/jev-1.13-free

SECRET HANDLING — this module holds no credential of its own:
  * No key is hardcoded, ever.
  * The TypeSafe key comes from the TYPESAFE_API_KEY environment variable.
  * The local key is read from `model.api_key` in ~/.hermes/config.yaml at call
    time. It stays in a local variable, is never logged, never returned, never
    placed in an exception message, and never written to disk.
  * Provider error bodies are redacted before they can reach a log or a tool
    result.

Egress: both backends send the `state` string (a task description or lesson
text) to the named endpoint. Nothing else is sent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("plugins.scelestine-adept.decision")

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
LOCAL_URL = "http://localhost:20128/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
LOCAL_MODEL = "oc/jev-1.13-free"

DEFAULT_TIMEOUT = 5.0      # well inside plugins.hook_callback_timeout (30s)
MAX_STATE_CHARS = 8000

# Only these question types are accepted by /systemone (probed on both backends).
# `boolean` returns 400 — use `noul` for yes/no.
VALID_TYPES = ("noul", "choice", "score")

# Redact anything key-shaped out of provider error text before it can be logged
# or returned in a tool result.
_SECRETISH = re.compile(
    r"(?i)(bearer\s+)\S+|(\bsk-[A-Za-z0-9]{6,})|\b([A-Za-z0-9_\-]{32,})\b"
)


def redact(text: str) -> str:
    """Mask credential-shaped substrings. Never raises."""
    if not isinstance(text, str) or not text:
        return ""
    try:
        out = _SECRETISH.sub(" <redacted> ", text)
    except re.error:
        return ""
    return out[:600]


def _typesafe_key() -> str:
    """TypeSafe key from the environment. Never logged."""
    try:
        return (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    except Exception:
        return ""


def _local_key() -> str:
    """Read `model.api_key` from ~/.hermes/config.yaml using stdlib only.

    PyYAML is not importable in every interpreter this plugin can be loaded
    under, so this walks lines looking for the top-level `model:` block. The
    value stays inside this function.
    """
    path = os.path.expanduser("~/.hermes/config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""

    in_model = False
    for line in lines:
        if line and not line[0].isspace() and not line.startswith("#") and ":" in line:
            in_model = line.split(":", 1)[0].strip() == "model"
            continue
        if not in_model:
            continue
        m = re.match(r"^\s+api_key:\s*(.+?)\s*$", line)
        if not m:
            continue
        raw = m.group(1).strip().strip("'\"")
        if not raw:
            return ""
        env = re.fullmatch(r"\$\{(\w+)\}", raw) or re.fullmatch(r"\$(\w+)", raw)
        if env:
            return (os.environ.get(env.group(1)) or "").strip()
        return raw
    return ""


def _post(url: str, payload: Dict[str, Any], key: str,
          timeout: float) -> Tuple[int, Dict[str, Any]]:
    """POST JSON. Returns (status, body). Never raises, never leaks `key`."""
    if not key:
        return 401, {"error": "no credential configured for this backend"}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Log the status only. The body is redacted before it can travel further.
        logger.debug("systemone HTTP %s (%s)", exc.code, url)
        try:
            detail = exc.read(400).decode("utf-8", "replace")
        except Exception:
            detail = ""
        return exc.code, {"error": redact(detail) or f"HTTP {exc.code}"}
    except Exception as exc:  # timeout, DNS, refused, malformed JSON...
        logger.debug("systemone %s failed: %s", type(exc).__name__, exc)
        return -1, {"error": type(exc).__name__}

    try:
        parsed = json.loads(body)
    except ValueError:
        logger.debug("systemone returned non-JSON (%s)", url)
        return -2, {"error": "non-JSON response"}
    if not isinstance(parsed, dict):
        return -2, {"error": "unexpected response shape"}
    return 200, parsed


def _backends(backend: str) -> List[Tuple[str, str, str, str]]:
    """Ordered (name, url, key, model) triples for the requested backend."""
    backend = (backend or "auto").lower()
    out: List[Tuple[str, str, str, str]] = []

    if backend in ("auto", "typesafe"):
        key = _typesafe_key()
        if key:
            out.append(("typesafe", TYPESAFE_URL, key, TYPESAFE_MODEL))
        elif backend == "typesafe":
            logger.debug("TYPESAFE_API_KEY not set")

    if backend in ("auto", "local"):
        key = _local_key()
        if key:
            out.append(("local", LOCAL_URL, key, LOCAL_MODEL))
        elif backend == "local":
            logger.debug("no local model.api_key found in config.yaml")

    return out


def ask(state: str, questions: Dict[str, Any], backend: str = "auto",
        timeout: float = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Run one SystemOne call. Returns the `answers` dict, or None (fail-open)."""
    if not isinstance(state, str) or not state.strip() or not questions:
        return None
    usable = {
        k: v for k, v in questions.items()
        if isinstance(v, dict) and v.get("type") in VALID_TYPES
    }
    if not usable:
        return None

    payload_state = state.strip()[:MAX_STATE_CHARS]
    for name, url, key, model in _backends(backend):
        code, body = _post(
            url, {"model": model, "state": payload_state, "questions": usable},
            key, timeout,
        )
        if code == 200 and isinstance(body.get("answers"), dict):
            logger.debug("systemone ok backend=%s questions=%d",
                         name, len(usable))
            return body["answers"]
        logger.debug("systemone backend=%s -> %s", name, code)
    return None


# --- convenience shapes -------------------------------------------------


def noul(state: str, instructions: str, backend: str = "auto",
         timeout: float = DEFAULT_TIMEOUT) -> Optional[float]:
    """Yes/no in 0..1. The workhorse — cheap, calibrated, and it works on both backends."""
    answers = ask(state, {"q": {"type": "noul", "instructions": instructions}},
                  backend=backend, timeout=timeout)
    if not answers:
        return None
    try:
        return float(answers["q"]["noul"])
    except (KeyError, TypeError, ValueError):
        return None


def rank(state: str, options: Dict[str, str], instruction: str,
         rubric: str = "", backend: str = "auto",
         timeout: float = DEFAULT_TIMEOUT) -> List[Tuple[str, float]]:
    """Shape B: one `choice` question with every candidate as a named option.

    `options` maps an opaque key to the candidate text. `instruction` is the
    question asked; `rubric` is appended to it as the scoring standard.
    Returns [(key, probability), ...] sorted high to low. Empty on any failure.

    Why Shape B and not one binary question per option (Shape A): measured on a
    5-case lesson-ranking task over a 7-option roster, both backends scored
    Shape B 5/5 top-1 and Shape A 2/5, and Shape B is one question instead of
    N. The option keys are opaque to the model, so all the signal rides in the
    criterion text — keep it verbatim.
    """
    if not options:
        return []
    if len(options) > 50:
        logger.debug("rank: roster %d > 50, truncating", len(options))
        options = dict(list(options.items())[:50])

    criteria: Dict[str, str] = {}
    for key, text in options.items():
        clean = " ".join(str(text).split())[:300]
        if not clean:
            continue
        criteria[str(key)] = clean
    if len(criteria) < 2:
        return []

    parts = [" ".join(str(instruction).split())]
    if rubric:
        parts.append(" ".join(str(rubric).split()))
    answers = ask(
        state,
        {"fit": {"type": "choice",
                 "instructions": " ".join(p for p in parts if p),
                 "criteria": criteria}},
        backend=backend, timeout=timeout,
    )
    if not answers:
        return []

    entry = answers.get("fit")
    if not isinstance(entry, dict):
        return []

    scored: List[Tuple[str, float]] = []
    probs = entry.get("probabilities")
    if isinstance(probs, dict):
        for key in criteria:
            try:
                scored.append((key, max(0.0, min(1.0, float(probs.get(key, 0.0))))))
            except (TypeError, ValueError):
                continue
    if not scored:
        # Some responses carry only `choice`. Give the winner a score and the
        # rest zero so callers still receive a usable ordering.
        winner = entry.get("choice")
        if isinstance(winner, str) and winner in criteria:
            scored = [(k, 1.0 if k == winner else 0.0) for k in criteria]

    scored.sort(key=lambda item: -item[1])
    return scored
