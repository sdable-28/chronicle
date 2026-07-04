# Chronicle

Feature lifecycle traceability for AI-assisted development.

Chronicle gives Claude a persistent memory of what worked, what failed, and why — across sessions, machines, and collaborators. It guards against re-proposing falsified experiments and briefs Claude at the start of every session.

## Requirements

- **Python 3.12+** — [python.org](https://python.org)
- **Claude Code** — [claude.ai/code](https://claude.ai/code) (for hooks and session briefs)
- **Node.js 18+** *(optional)* — required only for CodeGraph: [nodejs.org](https://nodejs.org)
- **uv** *(optional)* — required only for Graphify: `pip install uv`

## Install

```bash
pip install ai-chronicle
```

With semantic search (recommended):

```bash
pip install "ai-chronicle[embed]"
```

## Setup

```bash
# 1. Create the database
chronicle init

# 2. Wire Claude Code hooks + write ~/.claude/CLAUDE.md instructions
chronicle setup-hooks

# 3. (Optional) Embed existing events for semantic search
chronicle embed all
```

Restart Claude Code after step 2. From that point:
- **Session start**: Claude receives a brief of all FALSIFIED entities, validated edges, and recent decisions.
- **Session end**: Any `chronicle-log` blocks Claude emitted are auto-ingested.

## Companion tools

These are independent tools that Chronicle integrates with via Claude Code hooks:

```bash
# CodeGraph — exact file:line symbol lookup
npm install -g @colbymchenry/codegraph

# Graphify — relationship/concept knowledge graph
uv tool install graphifyy
# or: pip install graphifyy
```

## Core commands

```bash
# Exploration
chronicle brief [--full]                     # session summary: FALSIFIED, validated, recent
chronicle search "<query>" [--semantic]      # keyword or semantic search over past events
chronicle trace <entity>                     # full event history for one entity
chronicle status [--all]                     # entity table by status

# Logging
chronicle propose <entity> "<description>"   # PROPOSED
chronicle result <entity> "<description>"    # RESULT (with optional metrics)
chronicle validate <entity> "<description>"  # VALIDATED
chronicle falsify <entity> --finding "..."   # FALSIFIED — never re-propose
chronicle decide <entity> "<description>"    # DECISION
chronicle observe <entity> "<description>"   # OBSERVATION
chronicle wire <entity> -f file.py --line-start N   # WIRED — code location
chronicle verify [entity] [--root dir]       # check wired locations still exist

# Maintenance
chronicle embed all                          # embed events for semantic search
chronicle upgrade                            # apply schema migrations
chronicle init                               # create/re-initialise the database
```

## How to log from Claude

Claude emits fenced `chronicle-log` blocks during a response. The Stop hook ingests them automatically:

````
```chronicle-log
[
  {
    "entity": "my_experiment",
    "event": "RESULT",
    "description": "Caching layer A/B test: p50 latency 120ms → 38ms, cache hit rate 91%",
    "result_primary": 91.0,
    "metric_pct": 91.0,
    "gap": "Full load test not run yet"
  }
]
```
````

Or log directly from the CLI:

```bash
chronicle falsify my_experiment --finding "Adds 40ms overhead per request. Not worth it."
chronicle validate redis_cache --description "91% hit rate, p50 38ms, stable under 10k rps"
```

## Guard

When you (or Claude) name a new experiment, Chronicle warns if a similar FALSIFIED entity already exists:

```
GUARD WARNING: 'batch_cache_v2' matches existing entities:
  [FALSIFIED] batch_cache (name similarity 94%)

FALSIFIED entities cannot be recreated without --force.
This concept was proven not to work. See: chronicle trace batch_cache
```

## Entity lifecycle

```
PROPOSED -> IN_RESEARCH -> VALIDATED -> LIVE (wired in code)
                       \-> FALSIFIED  (proven not to work — guarded forever)
                       \-> SUPERSEDED (replaced by better approach)
```

## Database location

Default: `~/.chronicle/chronicle.db`

Override with `CHRONICLE_DB` environment variable.

## License

MIT
