# dev-loop · Dev Loop

> A closed-loop workflow for AI coding agents: requirement impact → change audit → doc sync → health check → defect diagnosis.
> Five stages wired into one loop, so you never hop between skills mid-task.

[中文](README.md) · [Install](#install) · [Usage](#usage) · [Output](#output) · [Limitations](#known-limitations)

---

## The problem

Most real-world defects are not "can't write the code". They are:

| Pain | What it looks like | What this skill does |
|---|---|---|
| Requirements arrive without an impact map | You change 3 files, miss the 7th, find out in production | S1 produces a change list that becomes the acceptance criteria |
| Regression testing is incomplete | You test the endpoint you touched, not its callers | S1 defines test scope, S2 lists missing tests |
| Code changes, docs don't | Docs still describe the interface from six months ago | S3 verifies paths, symbols and endpoints referenced in docs still exist |
| Code health is eyeballed | Debt accumulates until something breaks | S4 outputs P0/P1/P2 findings pinned to `file:line` |
| Error triage is guesswork | Three fix attempts before one lands | S5 forces an evidence chain before any fix |

## Why not just five prompts in one file

The value is **causality between stages**, not prompt concatenation:

```
Requirement → S1 Impact → change list (= acceptance criteria for S2)
                             │
                             ├→ Code → S2 Audit → missed changes / scope creep / missing tests
                             │                       │
                             └→ test scope           ▼
                                             S3 Doc sync (uses S2's diff)
                                                     │
                                     S4 Health check ◄┤
                                                     │
                                     S5 Diagnosis ◄──┘
                                            └→ always returns to S2
```

Without S1's list, S2 cannot detect a missed change — that's the step people skip, and it's the one that matters.
Without re-running S2 after S5, you miss the impact surface introduced by the fix itself.

## Install

The skill follows the common `SKILL.md` convention — drop it into your client's skills directory.

```bash
# user-level
git clone https://github.com/BHLO/dev_loop.git ~/.workbuddy/skills/dev-loop

# project-level (shared with the repo)
mkdir -p .workbuddy/skills
git clone https://github.com/BHLO/dev_loop.git .workbuddy/skills/dev-loop
```

> The directory name must be `dev-loop`, matching `name` in `SKILL.md`.
> Restart the client after installing.

**Requirements**: Python 3.8+, no third-party packages. Git optional — with git it analyses commits, without it use `--files`.

## Usage

Talk naturally:

```
"Orders need partial refunds — assess the impact."
  → impact matrix + change list + test scope + open questions

"Done editing. Did I miss anything?"
  → missed changes (P0) + scope creep + caller verdicts + missing tests

"Sync the docs."
  → per-document verdict + paragraph-level patches

"Check the health of this module."
  → P0/P1/P2 findings with file:line and fix suggestions

"Production error: AttributeError: 'NoneType' object has no attribute 'name'"
  → structure → stack mapping → root cause class → evidence → minimal fix
```

Or call the CLI directly:

```bash
python scripts/devloop.py next   --root .                        # what to do next
python scripts/devloop.py stage  --root . --stage S2 --base HEAD  # one command per stage
python scripts/devloop.py gate   --root . --stage S2 --approve --note "ok"
python scripts/devloop.py report --root . --history               # past report versions
```

## Output

Everything lands in `.devloop/` at the project root (add it to `.gitignore`):

```
.devloop/
├── profile.json      project profile: languages, build/test commands, docs, entrypoints
├── state.json        current stage, history, open items (resumable)
├── impact.md/json    change impact: callers / tests / docs / risk score
├── drift.md/json     stale paths, symbols and endpoints in docs
├── S1-impact.md      requirement impact + change list + test scope
├── S2-audit.md       missed changes / scope creep / caller verdicts / missing tests
├── S3-docs.md        per-document verdict and patches
├── S4-health.md      graded findings
└── S5-fix.md         symptom → root cause → fix → verification
```

## How it works

`scripts/devloop.py` does four mechanical things:

1. **Symbol extraction** — pulls affected functions/classes/routes from diff hunk context and top-level definitions.
2. **Reference scan** — compiles those symbols into one regex, scans the repo once, buckets hits into callers / tests / docs (~1.5s for 600 files).
3. **Risk scoring** — sums reference count, API/data/config surface, missing tests, churn and doc references into 0–100.
4. **Doc drift** — extracts inline code and fenced blocks from docs, checks whether referenced paths exist and symbols still appear in code.

Judgment work (semantic impact, root cause, fix design) is driven by the playbooks in `references/`, not guessed by the script.

## Known limitations

- **Python uses AST; other languages use regex.** Python symbols, routes and dynamic calls are exact;
  JS/TS/Java/Go stay approximate.
- **Dynamic calls can't be resolved — but they are listed.** `blind` reports reflection, DI containers,
  dynamic imports, `**kwargs` forwarding and string-built routes as explicit blind spots.
- **Cross-service impact is still manual.** The tool sees one repository; list inter-service calls
  in the S1 blind-spot section yourself.
- **`quality` catches obvious defects only** (secrets, SQL concatenation, swallowed exceptions,
  debug leftovers, missing tests). Passing it does not mean the code is good.
- **The risk score is a heuristic** for prioritisation, not a verdict on quality.
- **It won't edit your code.** S1–S4 are read-only.

## Contributing

Before opening a PR, run `scan` / `impact` / `gate` once to confirm the scripts still work.

The most valuable contribution is adding checks for your stack to `references/impact-rules.md`.

## License

MIT
