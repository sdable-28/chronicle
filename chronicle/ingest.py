"""
Parse and ingest CHRONICLE_LOG blocks.

Claude emits fenced blocks like:

    ```chronicle-log
    [
      {
        "entity": "redis_cache",
        "event": "RESULT",
        "description": "Cache A/B test, n=874 requests, hit rate 91%",
        "result_primary": 91.0,
        "metric_pct": 91.0,
        "n_samples": 874,
        "gap": "Full load test not run yet.",
        "script": "bench_cache.py",
        "when": "2026-06-04"
      }
    ]
    ```

Save to ~/.chronicle/pending.json via write_pending(), then call
chronicle ingest --file ~/.chronicle/pending.json
or set a Stop hook to auto-ingest.
"""

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import STATUS_TRANSITIONS, RESEARCH_EVENTS, resolve_entity, find_db

PENDING_FILE = Path.home() / ".chronicle" / "pending.json"

BLOCK_RE = re.compile(
    r"```chronicle-log\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)

EVENT_TYPE_ALIASES = {
    "RESULT": "RESULT",
    "PARTIAL": "PARTIAL_RESULT",
    "PARTIAL_RESULT": "PARTIAL_RESULT",
    "FALSIFIED": "FALSIFIED",
    "FALSIFY": "FALSIFIED",
    "VALIDATED": "VALIDATED",
    "VALIDATE": "VALIDATED",
    "WIRED": "WIRED",
    "WIRE": "WIRED",
    "DECISION": "DECISION",
    "DECIDE": "DECISION",
    "OBSERVATION": "OBSERVATION",
    "OBSERVE": "OBSERVATION",
    "EXPLORED": "EXPLORED",
    "EXPLORE": "EXPLORED",
    "PROPOSED": "PROPOSED",
    "NOTE": "NOTE",
    "TUNED": "TUNED",
    "REVERTED": "REVERTED",
    "SUPERSEDED": "SUPERSEDED",
    "DEPRECATED": "DEPRECATED",
    "PARAMETER_CHANGE": "PARAMETER_CHANGE",
    "COMBINED": "COMBINED",
    "WIRING_VERIFIED": "WIRING_VERIFIED",
    "WIRING_BROKEN": "WIRING_BROKEN",
}


def parse_blocks_from_text(text: str) -> list[dict]:
    entries = []
    for match in BLOCK_RE.finditer(text):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                entries.extend(data)
        except json.JSONDecodeError:
            pass
    return entries


def ingest_entries(entries: list[dict], conn: sqlite3.Connection) -> int:
    """Insert entries into DB. Returns count of events written."""
    count = 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    for entry in entries:
        # Honour review flag — "skip" excludes from import
        if entry.get("_review") == "skip":
            continue

        entity_raw = entry.get("entity") or entry.get("entity_id")
        if not entity_raw:
            continue

        canonical = resolve_entity(entity_raw, conn)
        if not canonical:
            entity_name = entry.get("entity_name") or entity_raw
            entity_type = entry.get("entity_type", "HYPOTHESIS")
            conn.execute(
                "INSERT OR IGNORE INTO entities(id, name, type, status, history_complete)"
                " VALUES (?,?,?,?,?)",
                (entity_raw, entity_name[:120], entity_type, "PROPOSED", 0),
            )
            canonical = entity_raw

        raw_type = str(entry.get("event") or entry.get("event_type") or "NOTE").upper()
        event_type = EVENT_TYPE_ALIASES.get(raw_type, "NOTE")

        event_at = entry.get("when") or entry.get("event_at") or now_iso
        expires_at = None
        if event_type == "PARTIAL_RESULT":
            days = int(entry.get("expires_days", 30))
            expires_at = (
                datetime.now(timezone.utc) + timedelta(days=days)
            ).strftime("%Y-%m-%dT%H:%M:%S")

        conn.execute(
            """
            INSERT INTO events(
                entity_id, event_type, event_at, description, finding, why,
                gap, next_step, result_primary, validation_primary,
                validation_holdout, metric_pct, n_samples, baseline,
                script, git_commit, external_run_id, conditions, expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                canonical,
                event_type,
                event_at,
                entry.get("description", ""),
                entry.get("finding"),
                entry.get("why"),
                entry.get("gap"),
                entry.get("next_step"),
                entry.get("result_primary") or entry.get("net"),
                entry.get("validation_primary") or entry.get("h1"),
                entry.get("validation_holdout") or entry.get("h2"),
                entry.get("metric_pct") or entry.get("wr"),
                entry.get("n_samples") or entry.get("n"),
                entry.get("baseline"),
                entry.get("script"),
                entry.get("git_commit") or entry.get("commit"),
                entry.get("external_run_id"),
                json.dumps(entry["conditions"]) if "conditions" in entry else None,
                expires_at,
            ),
        )

        # Update entity status for lifecycle events
        if event_type in STATUS_TRANSITIONS:
            new_status = STATUS_TRANSITIONS[event_type]
            conn.execute(
                "UPDATE entities SET status=? WHERE id=?",
                (new_status, canonical),
            )
        elif event_type in RESEARCH_EVENTS:
            conn.execute(
                "UPDATE entities SET status='IN_RESEARCH' WHERE id=? AND status='PROPOSED'",
                (canonical,),
            )

        # Handle extra metrics
        extra_metrics = entry.get("metrics", {})
        if isinstance(extra_metrics, dict):
            event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for name, value in extra_metrics.items():
                if isinstance(value, (int, float)):
                    conn.execute(
                        "INSERT INTO entity_metrics(entity_id,event_id,metric_name,metric_value_num) VALUES (?,?,?,?)",
                        (canonical, event_id, name, float(value)),
                    )
                else:
                    conn.execute(
                        "INSERT INTO entity_metrics(entity_id,event_id,metric_name,metric_value_str) VALUES (?,?,?,?)",
                        (canonical, event_id, name, str(value)),
                    )

        count += 1
    return count


def write_pending(entries: list[dict]) -> Path:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if PENDING_FILE.exists():
        try:
            existing = json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    existing.extend(entries)
    PENDING_FILE.write_text(json.dumps(existing, indent=2))
    return PENDING_FILE


def ingest_pending(conn: sqlite3.Connection) -> int:
    if not PENDING_FILE.exists():
        return 0
    try:
        entries = json.loads(PENDING_FILE.read_text())
        count = ingest_entries(entries, conn)
        PENDING_FILE.unlink()
        return count
    except Exception:
        return 0
