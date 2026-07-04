import difflib
import json
import sqlite3
from dataclasses import dataclass


SIMILARITY_THRESHOLD = 0.80


@dataclass
class GuardMatch:
    entity_id: str
    entity_name: str
    status: str
    score: float
    match_type: str          # 'NAME' | 'TAG_OVERLAP'
    overlapping_tags: list[str] | None = None


def run_guard(
    candidate_id: str,
    candidate_tags: list[str] | None,
    conn: sqlite3.Connection,
) -> list[GuardMatch]:
    """
    Check candidate against existing entities.
    Returns GuardMatch list — empty means safe to create.
    FALSIFIED matches are critical (status='FALSIFIED').
    """
    matches: list[GuardMatch] = []
    candidate_slug = candidate_id.lower().replace("_", " ").replace("-", " ")

    rows = conn.execute(
        "SELECT id, name, guard_tags, status FROM entities"
    ).fetchall()

    for row in rows:
        eid, ename, guard_tags_json, status = row

        # Name similarity — compare both slug and raw
        existing_slug = eid.lower().replace("_", " ").replace("-", " ")
        existing_name_slug = ename.lower().replace("_", " ").replace("-", " ")

        score = max(
            difflib.SequenceMatcher(None, candidate_slug, existing_slug).ratio(),
            difflib.SequenceMatcher(None, candidate_slug, existing_name_slug).ratio(),
        )

        # Prefix match: candidate is a substantial leading substring of existing name
        # (handles short queries against long imported entity slugs)
        if score < SIMILARITY_THRESHOLD and status == "FALSIFIED":
            min_len = min(len(candidate_slug), len(existing_slug))
            if min_len >= 12:  # ignore trivially short strings
                prefix_score = difflib.SequenceMatcher(
                    None, candidate_slug, existing_slug[:len(candidate_slug)]
                ).ratio()
                score = max(score, prefix_score * 0.95)  # slight discount for partial match

        if score >= SIMILARITY_THRESHOLD:
            matches.append(GuardMatch(
                entity_id=eid,
                entity_name=ename,
                status=status,
                score=round(score, 2),
                match_type="NAME",
            ))
            continue  # don't double-report via tag overlap for same entity

        # Tag overlap — only checked for FALSIFIED entities
        if status == "FALSIFIED" and guard_tags_json and candidate_tags:
            try:
                existing_tags = set(json.loads(guard_tags_json))
                candidate_set = set(t.strip().lower() for t in candidate_tags)
                overlap = existing_tags & candidate_set
                if overlap:
                    matches.append(GuardMatch(
                        entity_id=eid,
                        entity_name=ename,
                        status=status,
                        score=1.0,
                        match_type="TAG_OVERLAP",
                        overlapping_tags=sorted(overlap),
                    ))
            except (json.JSONDecodeError, TypeError):
                pass

    # Critical matches (FALSIFIED) first
    matches.sort(key=lambda m: (m.status != "FALSIFIED", -m.score))

    # Log FALSIFIED triggers for stats
    for m in matches:
        if m.status == "FALSIFIED":
            try:
                conn.execute(
                    "INSERT INTO guard_log(proposed, matched_entity, matched_status, similarity, match_type)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (candidate_id, m.entity_id, m.status, m.score, m.match_type),
                )
            except Exception:
                pass

    return matches


def format_guard_warning(candidate_id: str, matches: list[GuardMatch]) -> str:
    lines = [f"GUARD WARNING: '{candidate_id}' matches existing entities:"]
    for m in matches:
        flag = "[FALSIFIED]" if m.status == "FALSIFIED" else f"[{m.status}]"
        if m.match_type == "TAG_OVERLAP":
            detail = f"tag overlap: {', '.join(m.overlapping_tags or [])}"
        else:
            detail = f"name similarity {int(m.score * 100)}%"
        lines.append(f"  {flag} {m.entity_id} ({m.entity_name}) - {detail}")

    critical = [m for m in matches if m.status == "FALSIFIED"]
    if critical:
        lines.append("")
        lines.append("FALSIFIED entities cannot be recreated without --force.")
        lines.append("This concept was proven not to work. See: chronicle trace " + critical[0].entity_id)
    else:
        lines.append("")
        lines.append("Use --force to create anyway.")
    return "\n".join(lines)
