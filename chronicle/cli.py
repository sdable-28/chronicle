import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from .db import (
    STATUS_TRANSITIONS, RESEARCH_EVENTS,
    find_db, init_db, get_db, get_version, resolve_entity,
)
from .guard import run_guard, format_guard_warning
from .brief import format_brief
from .ingest import parse_blocks_from_text, ingest_entries, ingest_pending, PENDING_FILE


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _die(msg: str):
    click.echo(f"ERROR: {msg}", err=True)
    sys.exit(1)


def _capture_snippet(file_path: str, line_start: int | None) -> str | None:
    """Read 3 lines of context around line_start for later fingerprinting."""
    if not line_start:
        return None
    try:
        lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        lo = max(0, line_start - 2)
        hi = min(len(lines), line_start + 1)
        return "\n".join(lines[lo:hi])
    except Exception:
        return None


def _find_snippet_in_file(snippet: str, file_path: Path) -> int | None:
    """Search for snippet text in file; return 1-indexed line number of best match."""
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        needle = snippet.strip().splitlines()
        if not needle:
            return None
        first_line = needle[0].strip()
        for i, line in enumerate(lines):
            if line.strip() == first_line:
                # verify subsequent lines match too
                match = True
                for j, nl in enumerate(needle[1:], 1):
                    if i + j >= len(lines) or lines[i + j].strip() != nl.strip():
                        match = False
                        break
                if match:
                    return i + 1  # 1-indexed, pointing at first needle line
    except Exception:
        pass
    return None


def _git_find_rename(old_path: str, search_root: Path) -> str | None:
    """Ask git if old_path was renamed in recent history. Returns new path or None."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--diff-filter=R", "--name-status", "--format=", "-50", "--", old_path],
            cwd=str(search_root),
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].startswith("R"):
                return parts[2]  # new path
    except Exception:
        pass
    return None


def _run_verify_row(row, roots: list[Path], now: str, conn, dry_run: bool = False) -> str:
    """
    Run the smart decision tree on one wiring row.
    Returns 'ok', 'updated', or 'broken'.
    Writes DB updates unless dry_run=True.
    """
    file_str = row["file"]
    line_num  = row["line_start"]
    eid       = row["entity_id"]
    snippet   = row["content_snippet"] if "content_snippet" in row.keys() else None

    # Resolve file path
    resolved: Path | None = None
    p = Path(file_str)
    if p.is_absolute():
        resolved = p if p.exists() else None
    else:
        for root in roots:
            c = root / file_str
            if c.exists():
                resolved = c
                break

    # ── Part 1: file found ────────────────────────────────────────────────────
    if resolved is not None:
        if not line_num:
            if not dry_run:
                conn.execute("UPDATE wiring SET verified_at=?, last_check_at=? WHERE id=?",
                             (now, now, row["id"]))
                _log_event_impl(conn, eid, "WIRING_VERIFIED", f"verified: {file_str}")
            click.echo(f"  OK      {eid:<50}  {file_str}")
            return "ok"

        try:
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            lines = []

        if line_num <= len(lines):
            if not dry_run:
                conn.execute("UPDATE wiring SET verified_at=?, last_check_at=? WHERE id=?",
                             (now, now, row["id"]))
                _log_event_impl(conn, eid, "WIRING_VERIFIED", f"verified: {file_str}:{line_num}")
            click.echo(f"  OK      {eid:<50}  {file_str}:{line_num}")
            return "ok"

        # Line out of range — try content fingerprint
        if snippet:
            new_line = _find_snippet_in_file(snippet, resolved)
            if new_line:
                if not dry_run:
                    conn.execute(
                        "UPDATE wiring SET line_start=?, verified_at=?, last_check_at=? WHERE id=?",
                        (new_line, now, now, row["id"]),
                    )
                    _log_event_impl(conn, eid, "WIRING_VERIFIED",
                                    f"auto-corrected line drift: {file_str}:{line_num} -> :{new_line}")
                click.echo(f"  UPDATED {eid:<50}  {file_str}:{line_num} -> :{new_line}  (line drift)")
                return "updated"

        # No fingerprint match — broken
        if not dry_run:
            conn.execute("UPDATE wiring SET status='BROKEN', last_check_at=? WHERE id=?",
                         (now, row["id"]))
            _log_event_impl(conn, eid, "WIRING_BROKEN",
                            f"line {line_num} exceeds file length {len(lines)}")
        click.echo(f"  BROKEN  {eid:<50}  {file_str}:{line_num}  [line out of range]")
        return "broken"

    # ── Part 2: file not found — try git rename ───────────────────────────────
    new_path = _git_find_rename(file_str, roots[0])
    if new_path:
        new_resolved = roots[0] / new_path
        if not dry_run:
            conn.execute(
                "UPDATE wiring SET file=?, verified_at=?, last_check_at=? WHERE id=?",
                (new_path, now, now, row["id"]),
            )
            _log_event_impl(conn, eid, "WIRING_VERIFIED",
                            f"auto-corrected rename: {file_str} -> {new_path}")
        old_loc = f"{file_str}:{line_num}" if line_num else file_str
        new_loc = f"{new_path}:{line_num}" if line_num else new_path
        click.echo(f"  UPDATED {eid:<50}  {old_loc} -> {new_loc}  (git rename)")
        return "updated"

    # ── Part 3: truly broken ──────────────────────────────────────────────────
    if not dry_run:
        conn.execute("UPDATE wiring SET status='BROKEN', last_check_at=? WHERE id=?",
                     (now, row["id"]))
        _log_event_impl(conn, eid, "WIRING_BROKEN",
                        f"file not found, no git rename: {file_str}")
    loc = f"{file_str}:{line_num}" if line_num else file_str
    click.echo(f"  BROKEN  {eid:<50}  {loc}  [not found, no git rename]")
    click.echo(f"          -> chronicle wire {eid} -f <new_path> --line-start N")
    return "broken"


ENTITY_TYPES = ["STRATEGY", "EDGE", "HYPOTHESIS", "FEATURE", "OBSERVATION", "SYSTEM"]
ENTITY_STATUSES = ["PROPOSED", "IN_RESEARCH", "VALIDATED", "LIVE", "FALSIFIED", "DEPRECATED", "SUPERSEDED"]
ALL_EVENT_TYPES = [
    "PROPOSED", "VALIDATED", "FALSIFIED", "WIRED", "REVERTED", "SUPERSEDED", "DEPRECATED",
    "RESULT", "PARTIAL_RESULT", "EXPLORED", "TUNED", "PARAMETER_CHANGE", "COMBINED", "OBSERVATION",
    "DECISION", "NOTE", "WIRING_VERIFIED", "WIRING_BROKEN",
]


@click.group()
def cli():
    """Chronicle — feature lifecycle traceability."""


# ── init ──────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("db_path", required=False)
def init(db_path):
    """Create Chronicle DB. Default: ~/.chronicle/chronicle.db"""
    target = Path(db_path) if db_path else None
    path = init_db(target)
    click.echo(f"Initialized Chronicle DB: {path}")
    click.echo(f"Schema version: {get_version(path)}")


# ── new ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("entity_id")
@click.option("--name", "-n", help="Display name (defaults to entity_id)")
@click.option("--type", "-t", "entity_type", type=click.Choice(ENTITY_TYPES, case_sensitive=False), default="HYPOTHESIS")
@click.option("--tags", help="Comma-separated concept tags for FALSIFIED guard")
@click.option("--parent", help="Parent entity ID")
@click.option("--notes")
@click.option("--force", is_flag=True, help="Override guard warning")
@click.option("--no-history", "incomplete", is_flag=True, help="Mark history_complete=0 (pre-Chronicle)")
@click.option("--context", "pre_context", help="Context from before Chronicle was set up")
def new(entity_id, name, entity_type, tags, parent, notes, force, incomplete, pre_context):
    """Create a new entity with fuzzy FALSIFIED guard."""
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    display_name = name or entity_id

    with get_db() as conn:
        # Check for duplicates
        if resolve_entity(entity_id, conn):
            _die(f"Entity '{entity_id}' already exists. Use 'chronicle trace {entity_id}' to view it.")

        # Run fuzzy guard
        matches = run_guard(entity_id, tag_list, conn)
        if matches:
            click.echo(format_guard_warning(entity_id, matches))
            critical = [m for m in matches if m.status == "FALSIFIED"]
            if critical and not force:
                sys.exit(1)
            if not critical and not force:
                if not click.confirm("Create anyway?", default=False):
                    click.echo("Aborted.")
                    sys.exit(0)

        if parent and not resolve_entity(parent, conn):
            _die(f"Parent entity '{parent}' not found.")

        conn.execute(
            """INSERT INTO entities(id, name, type, status, parent_id, guard_tags,
               history_complete, pre_chronicle_context, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                entity_id,
                display_name,
                entity_type.upper(),
                "PROPOSED",
                parent,
                json.dumps(tag_list) if tag_list else None,
                0 if incomplete else 1,
                pre_context,
                notes,
            ),
        )
    hist_note = " [history_complete=0]" if incomplete else ""
    click.echo(f"Created: {entity_id} ({entity_type}){hist_note}")


# ── log (generic) ─────────────────────────────────────────────────────────────

def _log_event_impl(
    conn, entity_id, event_type, description,
    finding=None, why=None, gap=None, next_step=None,
    result_primary=None, validation_primary=None, validation_holdout=None,
    metric_pct=None, n_samples=None, baseline=None,
    script=None, git_commit=None, when=None, external_run_id=None,
    expires_days=None, conditions=None, extra_metrics=None,
    guard_tags=None,
):
    canonical = resolve_entity(entity_id, conn)
    if not canonical:
        # Auto-create with history_complete=0 (retroactive logging)
        conn.execute(
            "INSERT OR IGNORE INTO entities(id, name, type, status, history_complete) VALUES (?,?,?,?,?)",
            (entity_id, entity_id, "HYPOTHESIS", "PROPOSED", 0),
        )
        canonical = entity_id

    event_at = when or _now()
    expires_at = None
    if event_type == "PARTIAL_RESULT":
        days = expires_days or 30
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")

    cond_json = json.dumps(conditions) if isinstance(conditions, dict) else conditions

    conn.execute(
        """INSERT INTO events(
            entity_id, event_type, event_at, description, finding, why,
            gap, next_step, result_primary, validation_primary,
            validation_holdout, metric_pct, n_samples, baseline,
            script, git_commit, external_run_id, conditions, expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            canonical, event_type, event_at, description, finding, why,
            gap, next_step, result_primary, validation_primary,
            validation_holdout, metric_pct, n_samples, baseline,
            script, git_commit, external_run_id, cond_json, expires_at,
        ),
    )
    event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Update entity status
    if event_type in STATUS_TRANSITIONS:
        conn.execute(
            "UPDATE entities SET status=? WHERE id=?",
            (STATUS_TRANSITIONS[event_type], canonical),
        )
        # Update guard_tags on FALSIFIED
        if event_type == "FALSIFIED" and guard_tags:
            tag_list = [t.strip() for t in guard_tags.split(",")]
            conn.execute(
                "UPDATE entities SET guard_tags=? WHERE id=?",
                (json.dumps(tag_list), canonical),
            )
    elif event_type in RESEARCH_EVENTS:
        conn.execute(
            "UPDATE entities SET status='IN_RESEARCH' WHERE id=? AND status='PROPOSED'",
            (canonical,),
        )

    # Extra metrics
    if extra_metrics and isinstance(extra_metrics, dict):
        for mname, mval in extra_metrics.items():
            if isinstance(mval, (int, float)):
                conn.execute(
                    "INSERT INTO entity_metrics(entity_id,event_id,metric_name,metric_value_num) VALUES (?,?,?,?)",
                    (canonical, event_id, mname, float(mval)),
                )
            else:
                conn.execute(
                    "INSERT INTO entity_metrics(entity_id,event_id,metric_name,metric_value_str) VALUES (?,?,?,?)",
                    (canonical, event_id, mname, str(mval)),
                )
    return event_id


_LOG_OPTIONS = [
    click.option("--finding", "-f"),
    click.option("--why"),
    click.option("--gap"),
    click.option("--next-step", "next_step"),
    click.option("--net", "result_primary", type=float, help="Net result (trading: USD)"),
    click.option("--h1", "validation_primary", type=float, help="Primary validation result"),
    click.option("--h2", "validation_holdout", type=float, help="Holdout validation result"),
    click.option("--wr", "metric_pct", type=float, help="Win rate / accuracy pct"),
    click.option("--n", "n_samples", type=int, help="Sample count (trades, days, etc.)"),
    click.option("--baseline", type=float),
    click.option("--script"),
    click.option("--commit"),
    click.option("--when", help="event_at override (ISO date, default: now)"),
    click.option("--external-run-id", "external_run_id", help="MLflow/Neptune run ID"),
    click.option("--conditions", help="JSON conditions string"),
]


def add_log_options(f):
    for opt in reversed(_LOG_OPTIONS):
        f = opt(f)
    return f


@cli.command("log")
@click.argument("event_type", type=click.Choice(ALL_EVENT_TYPES, case_sensitive=False))
@click.argument("entity_id")
@click.option("--description", "-d", required=True)
@click.option("--tags", help="guard_tags to set (only used with FALSIFIED)")
@click.option("--expires-days", "expires_days", type=int, default=30, help="For PARTIAL_RESULT auto-expiry (default 30)")
@add_log_options
def log_event(event_type, entity_id, description, tags, expires_days,
              finding, why, gap, next_step, result_primary, validation_primary,
              validation_holdout, metric_pct, n_samples, baseline,
              script, commit, when, external_run_id, conditions):
    """Log any event type against an entity."""
    et = event_type.upper()
    cond = json.loads(conditions) if conditions else None
    with get_db() as conn:
        eid = _log_event_impl(
            conn, entity_id, et, description,
            finding=finding, why=why, gap=gap, next_step=next_step,
            result_primary=result_primary, validation_primary=validation_primary,
            validation_holdout=validation_holdout, metric_pct=metric_pct,
            n_samples=n_samples, baseline=baseline,
            script=script, git_commit=commit, when=when,
            external_run_id=external_run_id, expires_days=expires_days,
            conditions=cond, guard_tags=tags,
        )
    click.echo(f"Logged {et} on {entity_id} (event #{eid})")


# ── shorthand commands ────────────────────────────────────────────────────────

@cli.command("falsify")
@click.argument("entity_id")
@click.option("--finding", "-f", required=True, help="Key finding (one sentence)")
@click.option("--evidence", help="Supporting evidence")
@click.option("--tags", help="Comma-separated concept tags for future guard")
@click.option("--when")
def falsify(entity_id, finding, evidence, tags, when):
    """Mark entity as FALSIFIED (terminal). Sets guard_tags for future detection."""
    with get_db() as conn:
        _log_event_impl(
            conn, entity_id, "FALSIFIED", finding,
            finding=evidence, when=when, guard_tags=tags,
        )
    click.echo(f"FALSIFIED: {entity_id}")
    if tags:
        click.echo(f"  guard_tags set: {tags}")
    click.echo("  This entity cannot be recreated without --force.")


@cli.command("validate")
@click.argument("entity_id")
@click.option("--description", "-d", required=True)
@click.option("--net", "result_primary", type=float)
@click.option("--h1", "validation_primary", type=float)
@click.option("--h2", "validation_holdout", type=float)
@click.option("--wr", "metric_pct", type=float)
@click.option("--n", "n_samples", type=int)
@click.option("--baseline", type=float)
@click.option("--gap")
@click.option("--script")
@click.option("--commit")
@click.option("--when")
def validate(entity_id, description, result_primary, validation_primary,
             validation_holdout, metric_pct, n_samples, baseline, gap, script, commit, when):
    """Mark entity as VALIDATED (both halves passed)."""
    with get_db() as conn:
        _log_event_impl(
            conn, entity_id, "VALIDATED", description,
            result_primary=result_primary, validation_primary=validation_primary,
            validation_holdout=validation_holdout, metric_pct=metric_pct,
            n_samples=n_samples, baseline=baseline,
            gap=gap, script=script, git_commit=commit, when=when,
        )
    h_note = ""
    if validation_primary and validation_holdout:
        h_note = f"  H1=${validation_primary:,.0f}  H2=${validation_holdout:,.0f}"
    click.echo(f"VALIDATED: {entity_id}{h_note}")


@cli.command("explore")
@click.argument("entity_id")
@click.option("--description", "-d", required=True)
@click.option("--script")
@click.option("--when")
def explore(entity_id, description, script, when):
    """Log that we looked at data before any experiment."""
    with get_db() as conn:
        _log_event_impl(conn, entity_id, "EXPLORED", description, script=script, when=when)
    click.echo(f"EXPLORED: {entity_id}")


@cli.command("observe")
@click.argument("entity_id")
@click.option("--description", "-d", required=True)
@click.option("--when")
def observe(entity_id, description, when):
    """Log a data observation (e.g. 'April data has a regime shift')."""
    with get_db() as conn:
        _log_event_impl(conn, entity_id, "OBSERVATION", description, when=when)
    click.echo(f"OBSERVATION: {entity_id}")


@cli.command("decide")
@click.argument("entity_id")
@click.option("--description", "-d", required=True)
@click.option("--why")
@click.option("--when")
def decide(entity_id, description, why, when):
    """Log an architectural decision."""
    with get_db() as conn:
        _log_event_impl(conn, entity_id, "DECISION", description, why=why, when=when)
    click.echo(f"DECISION: {entity_id}")


# ── alias ─────────────────────────────────────────────────────────────────────

@cli.command("alias")
@click.argument("alias_name")
@click.argument("canonical_id")
@click.option("--note")
def alias(alias_name, canonical_id, note):
    """Map an alternate name to a canonical entity. E.g.: chronicle alias cache_v1 redis_cache"""
    with get_db() as conn:
        if not resolve_entity(canonical_id, conn):
            _die(f"Canonical entity '{canonical_id}' not found.")
        conn.execute(
            "INSERT OR REPLACE INTO entity_aliases(alias, canonical_id, note) VALUES (?,?,?)",
            (alias_name, canonical_id, note),
        )
    click.echo(f"Alias created: {alias_name} -> {canonical_id}")


# ── wire ──────────────────────────────────────────────────────────────────────

@cli.command("wire")
@click.argument("entity_id")
@click.option("--file", "-f", "file_path", required=True)
@click.option("--line-start", "line_start", type=int)
@click.option("--line-end", "line_end", type=int)
@click.option("--description")
@click.option("--commit")
def wire(entity_id, file_path, line_start, line_end, description, commit):
    """Record a code wiring location for an entity."""
    snippet = _capture_snippet(file_path, line_start)
    with get_db() as conn:
        canonical = resolve_entity(entity_id, conn)
        if not canonical:
            _die(f"Entity '{entity_id}' not found.")
        conn.execute(
            """INSERT INTO wiring(entity_id, file, line_start, line_end,
               description, git_commit, wired_at, content_snippet)
               VALUES (?,?,?,?,?,?,?,?)""",
            (canonical, file_path, line_start, line_end, description, commit, _now(), snippet),
        )
        # Log WIRED event
        _log_event_impl(
            conn, canonical, "WIRED",
            description or f"wired at {file_path}:{line_start or '?'}",
            git_commit=commit,
        )
    loc = f"{file_path}:{line_start or '?'}"
    click.echo(f"WIRED: {entity_id} -> {loc}")


# ── verify ────────────────────────────────────────────────────────────────────

@cli.command("verify")
@click.argument("entity_id", required=False)
@click.option("--root", "-r", "root_dir", default=None,
              help="Project root for resolving relative paths (default: cwd)")
def verify(entity_id, root_dir):
    """Check that wired file:line locations still exist on disk."""
    roots: list[Path] = [Path(root_dir).resolve() if root_dir else Path.cwd()]

    with get_db() as conn:
        q = "SELECT * FROM wiring WHERE status='ACTIVE'"
        params: list = []
        if entity_id:
            canonical = resolve_entity(entity_id, conn)
            if not canonical:
                _die(f"Entity '{entity_id}' not found.")
            q += " AND entity_id=?"
            params.append(canonical)
        rows = conn.execute(q, params).fetchall()

        if not rows:
            click.echo("No active wirings to verify.")
            return

        now = _now()
        ok_count = updated_count = broken_count = 0

        for row in rows:
            result = _run_verify_row(row, roots, now, conn)
            if result == "ok":
                ok_count += 1
            elif result == "updated":
                updated_count += 1
            else:
                broken_count += 1

        parts = [f"{ok_count} OK"]
        if updated_count:
            parts.append(f"{updated_count} auto-corrected")
        if broken_count:
            parts.append(f"{broken_count} BROKEN")
        click.echo(f"\n{', '.join(parts)}")


# ── realign ───────────────────────────────────────────────────────────────────

@cli.command("realign")
@click.option("--root", "-r", "root_dir", default=None,
              help="Project root for resolving relative paths (default: cwd)")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing")
def realign(root_dir, dry_run):
    """Scan all wired locations and auto-correct line drift and file renames."""
    roots: list[Path] = [Path(root_dir).resolve() if root_dir else Path.cwd()]

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM wiring WHERE status='ACTIVE'").fetchall()
        if not rows:
            click.echo("No active wirings.")
            return

        total = len(rows)
        click.echo(f"{'[DRY RUN] ' if dry_run else ''}Scanning {total} wired locations...\n")

        now = _now()
        ok_count = updated_count = broken_count = 0

        for row in rows:
            result = _run_verify_row(row, roots, now, conn, dry_run=dry_run)
            if result == "ok":
                ok_count += 1
            elif result == "updated":
                updated_count += 1
            else:
                broken_count += 1

        parts = [f"{ok_count} OK"]
        if updated_count:
            parts.append(f"{updated_count} auto-corrected")
        if broken_count:
            parts.append(f"{broken_count} need attention")
        click.echo(f"\n{', '.join(parts)}")
        if dry_run:
            click.echo("Re-run without --dry-run to apply.")


# ── trace ─────────────────────────────────────────────────────────────────────

@cli.command("trace")
@click.argument("entity_id")
@click.option("--full", is_flag=True, help="Include maintenance events")
def trace(entity_id, full):
    """Show full event history for an entity."""
    with get_db() as conn:
        canonical = resolve_entity(entity_id, conn)
        if not canonical:
            _die(f"Entity '{entity_id}' not found.")

        entity = conn.execute("SELECT * FROM entities WHERE id=?", (canonical,)).fetchone()
        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE canonical_id=?", (canonical,)
        ).fetchall()
        wiring_rows = conn.execute(
            "SELECT * FROM wiring WHERE entity_id=? AND status='ACTIVE' ORDER BY wired_at",
            (canonical,),
        ).fetchall()

        click.echo(f"\nENTITY: {canonical}")
        click.echo(f"  Name:    {entity['name']}")
        click.echo(f"  Type:    {entity['type']}")
        click.echo(f"  Status:  {entity['status']}")
        if not entity["history_complete"]:
            click.echo("  WARNING: history_complete=0 - events before Chronicle setup may be missing")
            if entity["pre_chronicle_context"]:
                click.echo(f"  Context: {entity['pre_chronicle_context']}")
        if aliases:
            click.echo(f"  Aliases: {', '.join(r['alias'] for r in aliases)}")
        click.echo(f"  Created: {entity['created_at'][:10]}")

        # Events
        q = """
            SELECT * FROM events WHERE entity_id=?
            ORDER BY event_at ASC, id ASC
        """
        events = conn.execute(q, (canonical,)).fetchall()

        if not events:
            click.echo("\nEVENTS: none")
        else:
            shown = [
                e for e in events
                if full or e["event_type"] not in ("WIRING_VERIFIED", "WIRING_BROKEN", "NOTE")
            ]
            click.echo(f"\nEVENTS ({len(shown)}):")
            for e in shown:
                _print_event(e)

        # Wiring
        if wiring_rows:
            click.echo("\nWIRING:")
            for w in wiring_rows:
                loc = f"{w['file']}:{w['line_start'] or '?'}"
                chk = w["last_check_at"][:10] if w["last_check_at"] else "never"
                click.echo(f"  {loc}  (last verified: {chk})")
        else:
            click.echo("\nWIRING: none")

        # Extra metrics
        metrics = conn.execute(
            "SELECT metric_name, metric_value_num, metric_value_str FROM entity_metrics WHERE entity_id=? ORDER BY logged_at",
            (canonical,),
        ).fetchall()
        if metrics:
            click.echo("\nMETRICS:")
            for m in metrics:
                val = m["metric_value_num"] if m["metric_value_num"] is not None else m["metric_value_str"]
                click.echo(f"  {m['metric_name']}: {val}")
        click.echo()


def _print_event(e):
    at = e["event_at"][:10] if e["event_at"] else "?"
    bi_note = ""
    if e["logged_at"] and e["event_at"] and e["logged_at"][:10] != e["event_at"][:10]:
        bi_note = f" (logged {e['logged_at'][:10]})"
    click.echo(f"\n  [{at}]{bi_note} {e['event_type']}")
    click.echo(f"    {e['description']}")
    for field in ("finding", "why", "gap", "next_step"):
        if e[field]:
            click.echo(f"    {field}: {e[field]}")
    metrics = []
    if e["result_primary"] is not None:
        metrics.append(f"net=${e['result_primary']:,.0f}")
    if e["validation_primary"] is not None:
        metrics.append(f"H1=${e['validation_primary']:,.0f}")
    if e["validation_holdout"] is not None:
        metrics.append(f"H2=${e['validation_holdout']:,.0f}")
    if e["metric_pct"] is not None:
        metrics.append(f"WR={e['metric_pct']:.1f}%")
    if e["n_samples"] is not None:
        metrics.append(f"n={e['n_samples']}")
    if metrics:
        click.echo(f"    {' | '.join(metrics)}")
    if e["script"]:
        click.echo(f"    script: {e['script']}")
    if e["git_commit"]:
        click.echo(f"    commit: {e['git_commit']}")
    if e["expires_at"] and e["event_type"] == "PARTIAL_RESULT":
        click.echo(f"    expires: {e['expires_at'][:10]}")


# ── guard (standalone check) ──────────────────────────────────────────────────

@cli.command("guard")
@click.argument("check_name")
@click.option("--tags", help="Comma-separated tags to check against FALSIFIED entities")
def guard(check_name, tags):
    """Check if a name would trigger the FALSIFIED guard without creating anything."""
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    with get_db() as conn:
        matches = run_guard(check_name, tag_list, conn)
    if matches:
        click.echo(format_guard_warning(check_name, matches))
        sys.exit(1)
    else:
        click.echo(f"CLEAR: '{check_name}' has no matches above 80% similarity.")


# ── search ────────────────────────────────────────────────────────────────────

@cli.command("search")
@click.argument("query")
@click.option("--semantic", is_flag=True, help="Use vector similarity (requires sqlite-vec)")
@click.option("--limit", type=int, default=10)
@click.option("--entity", help="Restrict to a specific entity")
def search(query, semantic, limit, entity):
    """Search events by text (FTS5) or semantic similarity."""
    with get_db() as conn:
        if semantic:
            from .embed import search_semantic, check_deps
            ok, msg = check_deps()
            if not ok:
                _die(msg)
            rows = search_semantic(query, conn, limit)
            if not rows:
                click.echo("No semantic matches found.")
                return
            click.echo(f"Semantic matches for '{query}':")
            for r in rows:
                dist = r.get("dist", "?")
                score = f"{1 - float(dist):.2f}" if isinstance(dist, (int, float)) else "?"
                click.echo(f"  [{score}] {r['entity_id']} | {r['event_type']} | {r['event_at'][:10]} | {r['description']}")
        else:
            entity_filter = ""
            params: list = [query]
            if entity:
                entity_filter = "AND ef.entity_id = ?"
                params.append(entity)

            # FTS5 search on event text
            fts_rows = conn.execute(
                f"""
                SELECT ef.rowid, e.entity_id, e.event_type, e.event_at, e.description
                FROM events_fts ef
                JOIN events e ON e.id = ef.rowid
                WHERE events_fts MATCH ?
                  {entity_filter}
                ORDER BY rank
                LIMIT {limit}
                """,
                params,
            ).fetchall()

            # Fallback: entity name/id per-word match — collect entities matching ANY query word
            words = [w.strip() for w in query.split() if w.strip()]
            matched_entity_ids: set[str] = set()
            for word in words:
                like_pat = f"%{word}%"
                ents = conn.execute(
                    "SELECT id FROM entities WHERE id LIKE ? OR name LIKE ?",
                    (like_pat, like_pat),
                ).fetchall()
                for ent in ents:
                    matched_entity_ids.add(ent["id"])
            name_rows: list = []
            for eid in matched_entity_ids:
                evs = conn.execute(
                    """
                    SELECT ev.id as rowid, ev.entity_id, ev.event_type, ev.event_at, ev.description
                    FROM events ev WHERE ev.entity_id = ?
                    ORDER BY ev.event_at DESC LIMIT ?
                    """,
                    (eid, limit),
                ).fetchall()
                name_rows.extend(evs)

            # Merge, deduplicate by event rowid
            seen: set[int] = set()
            rows = []
            for r in list(fts_rows) + list(name_rows):
                if r["rowid"] not in seen:
                    seen.add(r["rowid"])
                    rows.append(r)

            if not rows:
                click.echo(f"No results for '{query}'.")
                return
            click.echo(f"Results for '{query}':")
            for r in rows:
                click.echo(f"  {r['entity_id']:<24} | {r['event_type']:<16} | {r['event_at'][:10]} | {r['description']}")


# ── status ────────────────────────────────────────────────────────────────────

@cli.command("status")
@click.option("--all", "show_all", is_flag=True, help="Include FALSIFIED and DEPRECATED")
def status(show_all):
    """Show entity overview."""
    with get_db() as conn:
        q = "SELECT * FROM entities"
        if not show_all:
            q += " WHERE status NOT IN ('FALSIFIED','DEPRECATED')"
        q += " ORDER BY status, updated_at DESC"
        rows = conn.execute(q).fetchall()

        if not rows:
            click.echo("No entities found. Run 'chronicle new' to add your first.")
            return

        click.echo(f"\n{'ENTITY':<28} {'TYPE':<12} {'STATUS':<14} LAST UPDATED")
        click.echo("-" * 75)
        for r in rows:
            click.echo(
                f"  {r['id']:<26} {r['type']:<12} {r['status']:<14} {r['updated_at'][:10]}"
            )
        click.echo()

        # Counts by status
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        click.echo("  " + "  ".join(f"{k}:{v}" for k, v in sorted(counts.items())))
        click.echo()


# ── brief ─────────────────────────────────────────────────────────────────────

@cli.command("brief")
@click.option("--full", is_flag=True, help="Include IN_RESEARCH entities")
@click.option("--token-limit", "token_limit", type=int, default=500)
def brief(full, token_limit):
    """Generate SessionStart context brief."""
    with get_db() as conn:
        output = format_brief(conn, full=full, token_limit=token_limit)
    import sys
    sys.stdout.buffer.write(output.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")


# ── ingest ────────────────────────────────────────────────────────────────────

@cli.command("ingest")
@click.option("--file", "input_file", type=click.Path(), help="JSON file of log entries")
@click.option("--pending", is_flag=True, help="Process ~/.chronicle/pending.json")
@click.option("--stdin", "from_stdin", is_flag=True, help="Parse chronicle-log blocks from stdin")
def ingest(input_file, pending, from_stdin):
    """Ingest CHRONICLE_LOG entries from file, pending queue, or stdin."""
    with get_db() as conn:
        total = 0
        if pending:
            n = ingest_pending(conn)
            click.echo(f"Ingested {n} entries from pending.json")
            total += n
        if input_file:
            try:
                data = json.loads(Path(input_file).read_text(encoding="utf-8"))
                entries = data if isinstance(data, list) else [data]
                n = ingest_entries(entries, conn)
                click.echo(f"Ingested {n} entries from {input_file}")
                total += n
            except Exception as exc:
                _die(f"Failed to read {input_file}: {exc}")
        if from_stdin:
            text = sys.stdin.read()
            entries = parse_blocks_from_text(text)
            if entries:
                n = ingest_entries(entries, conn)
                click.echo(f"Ingested {n} entries from stdin blocks")
                total += n
            else:
                click.echo("No chronicle-log blocks found in stdin.")
        if total == 0 and not (pending or input_file or from_stdin):
            click.echo("Specify --file, --pending, or --stdin. See 'chronicle ingest --help'.")


# ── backfill ──────────────────────────────────────────────────────────────────

@cli.command("backfill")
@click.argument("entity_id")
@click.option("--context", "pre_context", help="Context from before Chronicle was set up")
@click.option("--complete", is_flag=True, help="Mark history_complete=1 after adding context")
def backfill(entity_id, pre_context, complete):
    """Mark entity as having pre-Chronicle history."""
    with get_db() as conn:
        canonical = resolve_entity(entity_id, conn)
        if not canonical:
            _die(f"Entity '{entity_id}' not found.")
        updates = []
        params = []
        if pre_context:
            updates.append("pre_chronicle_context=?")
            params.append(pre_context)
        if complete:
            updates.append("history_complete=1")
        if not updates:
            click.echo("Use --context to add pre-Chronicle context, --complete to mark as complete.")
            return
        params.append(canonical)
        conn.execute(
            f"UPDATE entities SET {', '.join(updates)} WHERE id=?", params
        )
    click.echo(f"Updated {entity_id}")


# ── session ───────────────────────────────────────────────────────────────────

@cli.command("session")
@click.argument("action", type=click.Choice(["start", "end", "list"]), default="list")
@click.option("--summary")
def session(action, summary):
    """Manage sessions. Use 'start' at session open, 'end' at close."""
    with get_db() as conn:
        if action == "start":
            sid = _now()
            conn.execute("INSERT INTO sessions(id) VALUES (?)", (sid,))
            click.echo(f"Session started: {sid}")
        elif action == "end":
            row = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                click.echo("No open session found.")
                return
            sid = row["id"]
            conn.execute(
                "UPDATE sessions SET ended_at=?, summary=? WHERE id=?",
                (_now(), summary, sid),
            )
            click.echo(f"Session ended: {sid}")
        else:  # list
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 20"
            ).fetchall()
            if not rows:
                click.echo("No sessions recorded.")
                return
            for r in rows:
                ended = r["ended_at"][:16] if r["ended_at"] else "open"
                click.echo(f"  {r['id'][:16]}  ended:{ended}  events:{r['events_logged']}")


# ── embed ─────────────────────────────────────────────────────────────────────

@cli.command("embed")
@click.argument("action", type=click.Choice(["all", "check"]), default="check")
def embed(action):
    """Manage semantic embeddings (requires sqlite-vec + sentence-transformers)."""
    from .embed import check_deps, embed_all
    ok, msg = check_deps()
    if action == "check":
        click.echo(f"Dependencies: {'OK' if ok else 'MISSING'}")
        if not ok:
            click.echo(f"  {msg}")
        return
    if not ok:
        _die(msg)
    with get_db() as conn:
        n = embed_all(conn)
    click.echo(f"Embedded {n} events.")


# ── upgrade ───────────────────────────────────────────────────────────────────

@cli.command("upgrade")
def upgrade():
    """Apply pending schema migrations to the Chronicle database."""
    from .db import apply_migrations
    current = get_version()
    click.echo(f"Chronicle DB: {find_db()}")
    click.echo(f"Current schema version: {current}")
    with get_db() as conn:
        applied = apply_migrations(conn)
    if applied:
        click.echo(f"Applied migrations: v{', v'.join(str(v) for v in applied)}")
        click.echo(f"Schema is now version {max(applied)}.")
    else:
        click.echo("Already up to date.")


# ── stats ─────────────────────────────────────────────────────────────────────

@cli.command("stats")
def stats():
    """Show usage metrics and estimated token savings."""
    with get_db() as conn:
        # Entities by status
        entity_rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM entities GROUP BY status ORDER BY n DESC"
        ).fetchall()
        total_entities = sum(r["n"] for r in entity_rows)

        # Events + sessions
        total_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        # Guard triggers (v2+ schema)
        try:
            guard_rows = conn.execute(
                "SELECT COUNT(*) as n, matched_entity FROM guard_log GROUP BY matched_entity"
            ).fetchall()
            guard_total = sum(r["n"] for r in guard_rows)
            guard_unique = len(guard_rows)
        except Exception:
            guard_total = None

        # Wiring
        wired = conn.execute(
            "SELECT COUNT(*) FROM wiring WHERE status='ACTIVE'"
        ).fetchone()[0]

    click.echo(f"\nChronicle DB: {find_db()}\n")

    click.echo(f"Entities     {total_entities} total")
    for r in entity_rows:
        click.echo(f"  {r['status']:<14} {r['n']}")

    click.echo(f"\nEvents       {total_events}  across {total_sessions} sessions")
    click.echo(f"Wired        {wired} active code locations")

    if guard_total is None:
        click.echo("\nGuard        run 'chronicle upgrade' to enable trigger tracking")
    elif guard_total == 0:
        click.echo("\nGuard        0 triggers (no re-proposals blocked yet)")
    else:
        low = guard_total * 10_000
        high = guard_total * 50_000
        click.echo(f"\nGuard        {guard_total} triggers  ({guard_unique} distinct entities blocked)")
        click.echo(f"Est. saved   ~{low//1000}k–{high//1000}k tokens")
    click.echo()


# ── setup-hooks ───────────────────────────────────────────────────────────────

_CLAUDE_MD_CHRONICLE = """
# Chronicle — Feature Lifecycle Tracker

Chronicle is a local SQLite-backed tool that tracks research decisions, validated edges,
and falsified experiments across AI-assisted development sessions.

## What it does

- **Guards** against re-proposing falsified ideas (fuzzy-match warning on entity create)
- **Briefs** at session start: FALSIFIED list, open research threads, recent decisions
- **Searches** past decisions semantically: `chronicle search "..." --semantic`
- **Traces** the full history of any entity: `chronicle trace <entity>`

## Session start

The SessionStart hook runs `chronicle brief` automatically. The injected output contains:
- FALSIFIED entities — ideas proven not to work, **never re-propose these**
- VALIDATED edges — confirmed working approaches
- DECISIONS — architectural choices and rules
- Open threads — work in progress

## Logging events during a session

Emit a fenced block anywhere in your response and the Stop hook auto-ingests it:

```chronicle-log
[
  {
    "entity": "my_experiment",
    "event": "RESULT",
    "description": "What was found",
    "result_primary": 91.0,
    "metric_pct": 91.0,
    "gap": "H1/H2 split not yet run"
  }
]
```

**Event types:** PROPOSED, RESULT, PARTIAL_RESULT, EXPLORED, VALIDATED, FALSIFIED,
WIRED, REVERTED, DECISION, OBSERVATION, NOTE, TUNED, PARAMETER_CHANGE

## Key commands

```
chronicle brief [--full]                    # session summary
chronicle search "<query>" [--semantic]     # find past decisions
chronicle trace <entity>                    # full history of one entity
chronicle falsify <entity> --finding "..."  # mark as proven not to work
chronicle validate <entity> --description "..."
chronicle wire <entity> -f file.py --line-start N
chronicle verify [entity] [--root dir]      # check wired code still exists
chronicle upgrade                           # apply schema migrations
chronicle embed all                         # (re)embed events for semantic search
```

## Guard

When proposing a new experiment or entity, Chronicle warns if a similar FALSIFIED entity
already exists. A warning means: **stop and check `chronicle trace <entity>` first**.
FALSIFIED entities require `--force` to recreate — don't bypass without reading the trace.
"""


def _merge_hooks(settings: dict, event: str, new_group: dict) -> None:
    """Append a hook group to an event list, avoiding exact duplicates."""
    hooks_dict = settings.setdefault("hooks", {})
    existing = hooks_dict.setdefault(event, [])
    # Deduplicate by comparing the inner command strings
    new_cmds = {h["command"] for h in new_group.get("hooks", [])}
    for grp in existing:
        existing_cmds = {h["command"] for h in grp.get("hooks", [])}
        if existing_cmds == new_cmds:
            return  # already present
    existing.append(new_group)


@cli.command("setup-hooks")
@click.option("--claude-md/--no-claude-md", default=True,
              help="Write Chronicle instructions to ~/.claude/CLAUDE.md (default: yes)")
def setup_hooks(claude_md):
    """Wire Chronicle's SessionStart and Stop hooks into ~/.claude/settings.json.

    Also writes Chronicle usage instructions to ~/.claude/CLAUDE.md so Claude
    understands the tool on any machine.
    """
    import json as _json
    import sys as _sys

    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Load or init settings
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = _json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    py = _sys.executable.replace("\\", "/")

    # SessionStart — run `chronicle brief` and inject output
    session_start_cmd = (
        f'{py} -c "'
        r'import subprocess,json,sys; '
        r'r=subprocess.run([\"chronicle\",\"brief\"],capture_output=True,text=True,'
        r'encoding=\"utf-8\",errors=\"replace\"); '
        r'print(json.dumps({\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",'
        r'\"additionalContext\":r.stdout.strip()}}))'
        f'" 2>/dev/null || echo \'{{}}\''
    )
    _merge_hooks(settings, "SessionStart", {
        "hooks": [{"type": "command", "command": session_start_cmd, "timeout": 15}]
    })

    # Stop — auto-ingest any pending chronicle-log blocks
    stop_cmd = "chronicle ingest --pending 2>/dev/null || true"
    _merge_hooks(settings, "Stop", {
        "hooks": [{"type": "command", "command": stop_cmd, "timeout": 10}]
    })

    settings_path.write_text(
        _json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    click.echo(f"Hooks written to {settings_path}")

    # ~/.claude/CLAUDE.md
    if claude_md:
        claude_md_path = Path.home() / ".claude" / "CLAUDE.md"
        marker = "# Chronicle"
        existing_text = claude_md_path.read_text(encoding="utf-8") if claude_md_path.exists() else ""
        if marker in existing_text:
            click.echo(f"~/.claude/CLAUDE.md already contains Chronicle section — skipped.")
        else:
            with open(claude_md_path, "a", encoding="utf-8") as f:
                f.write("\n" + _CLAUDE_MD_CHRONICLE.strip() + "\n")
            click.echo(f"Chronicle instructions appended to {claude_md_path}")

    click.echo("")
    click.echo("Done. Restart Claude Code (or open a new session) to activate the hooks.")
    click.echo("Verify with: chronicle brief")
