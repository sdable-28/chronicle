import sqlite3
from datetime import datetime, timezone


def _token_est(text: str) -> int:
    return max(1, len(text) // 4)


def format_brief(
    conn: sqlite3.Connection,
    full: bool = False,
    token_limit: int = 500,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sections: list[str] = []

    # ── FALSIFIED — always included, never truncated ──────────────────────────
    falsified_rows = conn.execute("""
        SELECT e.id, e.name, ev.description, ev.event_at, ev.finding, e.guard_tags
        FROM entities e
        JOIN events ev ON ev.entity_id = e.id AND ev.event_type = 'FALSIFIED'
        WHERE e.status = 'FALSIFIED'
        ORDER BY ev.event_at DESC
    """).fetchall()

    if falsified_rows:
        lines = ["-- FALSIFIED (never repropose) --"]
        for r in falsified_rows:
            guard_note = ""
            if r["guard_tags"]:
                import json
                try:
                    tags = json.loads(r["guard_tags"])
                    guard_note = f"  tags:{','.join(tags)}"
                except Exception:
                    pass
            summary = r["description"] or r["finding"] or ""
            lines.append(
                f"  {r['id']:<24} | {r['event_at'][:10]} | {summary}{guard_note}"
            )
        sections.append("\n".join(lines))

    # ── LIVE / VALIDATED changed last 7 days ─────────────────────────────────
    live_rows = conn.execute("""
        SELECT e.id, e.name, e.status, ev.event_type, ev.description, ev.event_at,
               ev.result_primary, ev.validation_primary, ev.validation_holdout
        FROM entities e
        JOIN events ev ON ev.entity_id = e.id
        WHERE e.status IN ('LIVE', 'VALIDATED')
          AND ev.logged_at >= datetime('now', '-7 days')
        ORDER BY ev.logged_at DESC
    """).fetchall()

    seen_live: set[str] = set()
    if live_rows:
        lines = ["-- LIVE / VALIDATED (changed last 7d) --"]
        for r in live_rows:
            if r["id"] in seen_live:
                continue
            seen_live.add(r["id"])
            metrics = ""
            if r["validation_primary"] is not None and r["validation_holdout"] is not None:
                metrics = f"  H1=${r['validation_primary']:,.0f} H2=${r['validation_holdout']:,.0f}"
            elif r["result_primary"] is not None:
                metrics = f"  net=${r['result_primary']:,.0f}"
            lines.append(
                f"  {r['id']:<24} | {r['status']:<10} | {r['event_at'][:10]} | {r['description']}{metrics}"
            )
        if len(lines) > 1:
            sections.append("\n".join(lines))

    # ── PARTIAL_RESULT — H2 pending ───────────────────────────────────────────
    partial_rows = conn.execute("""
        SELECT e.id, ev.description, ev.event_at, ev.expires_at,
               ev.validation_primary, ev.validation_holdout
        FROM entities e
        JOIN events ev ON ev.entity_id = e.id AND ev.event_type = 'PARTIAL_RESULT'
        WHERE ev.expires_at > datetime('now')
        ORDER BY ev.expires_at ASC
    """).fetchall()

    if partial_rows:
        lines = ["-- PARTIAL RESULT (H2 pending) --"]
        for r in partial_rows:
            exp = r["expires_at"][:10] if r["expires_at"] else "?"
            h1 = f"H1=${r['validation_primary']:,.0f}" if r["validation_primary"] is not None else "H1=?"
            lines.append(
                f"  {r['id']:<24} | {r['event_at'][:10]} | {r['description']} | {h1} | expires {exp}"
            )
        sections.append("\n".join(lines))

    # ── EXPIRED PARTIAL_RESULT ────────────────────────────────────────────────
    expired_rows = conn.execute("""
        SELECT e.id, ev.description, ev.expires_at
        FROM entities e
        JOIN events ev ON ev.entity_id = e.id AND ev.event_type = 'PARTIAL_RESULT'
        WHERE ev.expires_at <= datetime('now')
          AND e.status != 'FALSIFIED'
          AND e.status != 'VALIDATED'
        ORDER BY ev.expires_at ASC
    """).fetchall()

    if expired_rows:
        lines = ["-- EXPIRED PARTIAL_RESULT (needs FALSIFIED or VALIDATED) --"]
        for r in expired_rows:
            lines.append(
                f"  {r['id']:<24} | expired {r['expires_at'][:10]} | {r['description']}"
            )
        sections.append("\n".join(lines))

    # ── STALE WIRING ──────────────────────────────────────────────────────────
    stale_rows = conn.execute("""
        SELECT e.id, w.file, w.line_start, w.last_check_at, w.verified_at
        FROM entities e
        JOIN wiring w ON w.entity_id = e.id AND w.status = 'ACTIVE'
        WHERE e.status = 'LIVE'
          AND (w.last_check_at < datetime('now', '-14 days') OR w.last_check_at IS NULL)
        ORDER BY w.last_check_at ASC NULLS FIRST
    """).fetchall()

    if stale_rows:
        lines = ["-- STALE WIRING (verify before editing) --"]
        for r in stale_rows:
            loc = f"{r['file']}:{r['line_start'] or '?'}"
            last = r["last_check_at"][:10] if r["last_check_at"] else "never"
            lines.append(f"  {r['id']:<24} | {loc} | last checked {last}")
        sections.append("\n".join(lines))

    # ── Full mode: IN_RESEARCH entities ───────────────────────────────────────
    if full:
        research_rows = conn.execute("""
            SELECT e.id, e.name, e.status, e.updated_at,
                   ev.event_type, ev.description, ev.event_at
            FROM entities e
            LEFT JOIN events ev ON ev.id = (
                SELECT id FROM events WHERE entity_id = e.id ORDER BY logged_at DESC LIMIT 1
            )
            WHERE e.status IN ('IN_RESEARCH', 'PROPOSED')
            ORDER BY e.updated_at DESC
        """).fetchall()

        if research_rows:
            lines = ["-- IN RESEARCH --"]
            for r in research_rows:
                et = r["event_at"][:10] if r["event_at"] else "?"
                lines.append(
                    f"  {r['id']:<24} | {r['status']:<12} | last: {et} {r['event_type'] or ''} | {r['description'] or ''}"
                )
            sections.append("\n".join(lines))

    if not sections:
        body = "  (no entries yet - run 'chronicle new' to add your first entity)"
    else:
        body = "\n\n".join(sections)

    header = f"=== CHRONICLE [{now}] ==="
    footer = "=" * len(header)
    result = f"{header}\n\n{body}\n\n{footer}"

    # Truncate to token_limit (never cut FALSIFIED section)
    if not full and _token_est(result) > token_limit:
        # Re-build with only FALSIFIED + note about truncation
        result = (
            f"{header}\n\n"
            + (sections[0] if sections else "")
            + f"\n\n(brief truncated - run 'chronicle brief --full' for complete view)\n\n"
            + footer
        )

    return result
