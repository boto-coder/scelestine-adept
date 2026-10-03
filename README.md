# scelestine-adept

Self-learning for Scelestine, a Hermes Agent plugin.

It closes the loop most agents never close: **record** what went wrong, **recall**
it when the next task looks like the same shape, and **distill** repeated lessons
into standing behaviors that live in `SOUL.md`.

Storage is **Mnemosyne only** — no JSONL, no local lesson file, no side database.
The only file it writes is a clearly marked block inside `SOUL.md`, and it never
touches a byte outside that block.

---

## The loop

```
            ┌────────────────────────── nightly cron (03:00) ──────────────────────────┐
            │                                                                          │
            ▼                                                                          │
   adept_remember ──► Mnemosyne ──► adept_recall ──► inject top 3 ──► the model      │
        ▲                │             ▲              (pre_llm_call)                  │
        │                │             │                                              │
   post_tool_call ───────┘        Jev re-rank                                          │
   (auto-record on failure)                                                          │
                                                                                         │
            └────────────── adept_reflect / adept_review ──► SOUL.md ◄──────────────────┘
```

1. **Record.** `adept_remember` writes a lesson to Mnemosyne. `post_tool_call`
   also auto-records a lesson when a tool fails in a way Jev judges worth keeping.
2. **Recall.** `pre_llm_call` pulls the current turn through Mnemosyne hybrid
   retrieval, re-ranks the shortlist with one Jev System One call, and injects
   the top 3 lessons into the prompt.
3. **Distill.** Every night at 03:00 the cron job promotes at most 2 recurring
   lessons into standing behaviors, which are projected into a managed block in
   `SOUL.md`.

---

## Install

```bash
git clone git@github.com:boto-coder/scelestine-adept.git
cp -r scelestine-adept ~/.hermes/plugins/scelestine-adept
hermes plugins enable scelestine-adept
hermes gateway restart          # restarts live agents — do it deliberately
```

Confirm:

```bash
hermes plugins ls | grep -i scelestine
```

### Requirements

| Requirement | Why |
|---|---|
| Hermes >= 0.21.0 | `pre_llm_call` hook and `ctx.get_config` |
| Mnemosyne plugin enabled | the only storage backend this plugin uses |
| `TYPESAFE_API_KEY` in the environment | primary Jev ranking backend |

Without the key the plugin still works: recall falls back to Mnemosyne's own
ordering, auto-record skips its quality gate, and nothing errors.

---

## The four tools

| Tool | What it does |
|---|---|
| `adept_remember` | Record one reusable lesson. Detects duplicates and reports them instead of saving twice. Estimates importance with a Jev call when you do not supply it. |
| `adept_recall` | Rank past lessons for a task: Mnemosyne hybrid retrieval → Jev re-rank → top 3 above the confidence floor. |
| `adept_reflect` | Distill one standing behavior into a `SOUL.md` managed slot, through a safety gate. Optionally promotes a source memory into the long-term persona store. |
| `adept_review` | Nightly review entry point. Scores recent lessons against a promotion rubric, promotes the winners, enforces the behavior cap. Supports `dry_run`. |

`adept_reflect` writes only to slots named `rule-01` … `rule-10`, inside a block
delimited by these markers:

```html
<!-- scelestine-adept:managed:start -->
...
<!-- scelestine-adept:managed:end -->
```

Everything outside those markers is user-owned and is never modified. If the
markers are inconsistent (a stray or out-of-order marker), the write is refused
rather than risk swallowing text.

---

## The two hooks

| Hook | Direction | Behaviour |
|---|---|---|
| `pre_llm_call` | injects | Appends a `LESSONS FROM PAST WORK` block with the top 3 ranked lessons. Skips short messages, slash commands, and any platform in `skip_platforms`. |
| `post_tool_call` | observes | On a detected tool failure, asks Jev whether the failure is a reusable lesson and records it if it clears the floor. The return value is ignored — it is an observer only. |

Every hook body is wrapped: any exception is logged and the turn continues.
A Jev outage, a missing credential, or a Mnemosyne timeout all degrade to
"nothing is injected", never to a blocked turn.

---

## Configuration

Set under `plugins.config.scelestine-adept` in `~/.hermes/config.yaml`:

```yaml
plugins:
  config:
    scelestine-adept:
      backend: auto              # auto | typesafe | local
      inject_top: 3              # lessons injected per turn
      shortlist: 12              # Mnemosyne candidates before re-ranking
      confidence_floor: 0.55     # drop lessons Jev scores below this
      auto_record: true          # post_tool_call writes lessons on failure
      auto_record_floor: 0.60    # Jev gate for whether a failure is a lesson
      jev_safety_gate: true      # second-layer rule gate in adept_reflect
      min_message_chars: 20      # ignore greetings for injection
      skip_platforms: []         # e.g. ["telegram"]
```

---

## Ranking: how it chooses

Recall uses **one Jev System One `choice` question with every shortlisted lesson
as a named option**, and reads the resulting probabilities.

The first implementation used one binary question per option instead. Measured
on a 5-case lesson-ranking task over a 7-option roster, that scored **2/5 top-1**;
the single-`choice` shape scored **5/5 top-1 on both backends**, and it costs one
question instead of N. The current code uses the winner.

Verified after the rewrite: **6/6 top-1 and 6/6 top-3** across 3 tasks × 2
backends, with clean separation (the winning lesson at `1.000` against `0.000`
for irrelevant ones in most cases), at 280–440 ms per call.

| Backend | Endpoint | Model | Role |
|---|---|---|---|
| TypeSafe | `https://api.typesafe.ai/v1/systemone` | `jev-latest` | primary |
| Local | `http://localhost:20128/v1/systemone` | `oc/jev-1.13-free` | fallback |

Only `noul`, `choice`, and `score` question types are accepted by the API —
`boolean` returns 400. Ranking uses `choice`; the yes/no gates use `noul`.

---

## Secrets and egress

**There are no secrets in this repository.** Verified by an automated scan for
credential-shaped literals (`sk-…`, `ghp_…`, `AKIA…`, bearer assignments, keys in
URLs, long hex blobs, private key blocks) returning zero hits.

| Topic | Behaviour |
|---|---|
| Hardcoded keys | None, in any file. |
| TypeSafe key | Read from the `TYPESAFE_API_KEY` environment variable at call time. Never logged, never returned, never written to disk. |
| Local key | Read from `model.api_key` in `~/.hermes/config.yaml` at call time, kept in a local variable. |
| Error bodies | Redacted before they can reach a log line or a tool result — bearer tokens, `sk-` keys, and 32+ character tokens are replaced with `<redacted>`. |
| Network | Only the two endpoints in the table above. No other outbound call. |
| Shell | No `subprocess`, no `os.system`, no `eval`/`exec`. Requests use stdlib `urllib`. |
| Files written | Exactly one: `~/.hermes/SOUL.md`, atomically via a temp file and `os.replace`. |
| What leaves the machine | The `state` string — a task description or a lesson text — to the chosen Jev endpoint. Nothing else. |

### The rule safety gate

A standing behavior is a durable instruction that runs every session, so
`adept_reflect` puts every rule through two layers before it is written:

1. **Rule checks** — length bounds, plus patterns for instruction-override
   phrasing (`ignore all previous instructions`), exfiltration
   (`send the api key to …`), destructive shell (`| sh`), and secrecy
   (`do not tell the user`).
2. **Jev gate** — a `noul` question asking whether the rule smuggles an
   instruction. Skippable with `jev_safety_gate: false`.

A rejected rule is reported with its reason and nothing is written.

---

## Verification

The test harness loads the package the same way
`hermes_cli/plugins_loader.py::_load_directory_module` does, so relative imports
resolve exactly as they do at runtime.

```bash
python3 test_scelestine.py      # 111/111 — handlers, hooks, gates, file safety
python3 test_live_rank.py       # live re-ranking, both backends
```

What is covered:

- `register(ctx)` registers 4 tools and 2 hooks and performs no dispatch.
- Fail-open: dead endpoints, missing credentials, and raised exceptions all
  return `None`/`[]` rather than raising.
- Hook behaviour: platform skip, short-message skip, slash-command skip,
  exception swallowing, and auto-record dedupe.
- Failure detection across the status/error/result shapes the hook contract ships.
- `SOUL.md` byte safety: with the managed block removed, the result equals the
  input — including after 5 consecutive rewrites, which caught a real bug where
  one newline leaked per rewrite.
- Corrupt-marker refusal: a stray marker leaves the file untouched.
- Live ranking shape, sort order, score range, and round-trip of option text.

---

## Repository layout

```
scelestine-adept/
├── plugin.yaml     manifest: hooks, tools, version, requirements
├── README.md       this file
├── __init__.py     register(), 4 tool handlers, 2 hooks, config
├── schemas.py      pure data — tool JSON schemas, no sibling imports
├── decision.py     Jev System One client, dual backend, fail-open, redaction
├── store.py        Mnemosyne adapter (remember / recall / canonical / persona)
├── identity.py     SOUL.md managed block + the rule safety gate
├── recall.py       retrieval shortlist → Jev re-rank → injection block
└── review.py       nightly promotion rubric and behavior cap
```

Design notes: the plugin tree is treated as one package, so sibling modules are
imported as `from . import decision`. `register()` performs registrations only —
no network calls, no file writes at load time.

---

## Credits

Built for [Hermes Agent](https://hermes-agent.nousresearch.com/docs/plugins/)
by Nous Research. Ranking uses [TypeSafe System One](https://docs.typesafe.ai/).
Storage uses [Mnemosyne](https://hermes-agent.nousresearch.com/docs/).
