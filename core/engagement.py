"""
Engagement normalization — computes a 0-100 percentile score for each comment's
raw like_count (or Reddit score), within its source_type group for the community.

This makes engagement comparable across platforms for the pipeline's confidence
thresholds and ranking logic.

Runs incrementally: only comments with NULL engagement_normalized are updated.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def normalize_engagement(conn, community_id: int) -> int:
    """
    Compute engagement_normalized (0-100 percentile) for all comments in this
    community that don't have a score yet.

    Uses within-source-type percentile rank of like_count among all comments
    belonging to the community's sources of that type.

    Returns the number of comments updated.
    """
    # Get source_ids for each source_type in this community
    sources = conn.execute(
        "SELECT source_type, source_id FROM community_sources WHERE community_id = ?",
        (community_id,),
    ).fetchall()

    if not sources:
        # Fallback: legacy community_channels (all YouTube)
        channel_ids = [
            r["channel_id"] for r in conn.execute(
                "SELECT channel_id FROM community_channels WHERE community_id = ?",
                (community_id,),
            ).fetchall()
        ]
        sources = [
            type("Row", (), {"source_type": "youtube", "source_id": cid})()
            for cid in channel_ids
        ]

    # Group source_ids by source_type
    by_type: dict[str, list[str]] = {}
    for row in sources:
        by_type.setdefault(row["source_type"], []).append(row["source_id"])

    total_updated = 0
    for source_type, source_ids in by_type.items():
        placeholders = ",".join("?" * len(source_ids))
        # Count total comments of this source_type in the community
        total = conn.execute(
            f"SELECT COUNT(*) FROM comments "
            f"WHERE source_type = ? AND channel_id IN ({placeholders})",
            [source_type] + source_ids,
        ).fetchone()[0]

        if total == 0:
            continue

        # Fetch IDs + like_count for comments missing normalization
        rows = conn.execute(
            f"SELECT comment_id, like_count FROM comments "
            f"WHERE source_type = ? AND channel_id IN ({placeholders}) "
            f"AND engagement_normalized IS NULL",
            [source_type] + source_ids,
        ).fetchall()

        if not rows:
            continue

        log.info(
            f"Normalizing {len(rows)} comments for source_type={source_type!r} "
            f"(total={total})"
        )

        # For each comment, count how many in the same group have like_count <=
        # This is O(n²) but fine for typical community sizes (<100k comments).
        # For very large datasets, a single ranking query is used instead.
        if len(rows) > 5000:
            # Bulk approach: rank all comments at once
            updated = _normalize_bulk(conn, source_type, source_ids)
        else:
            updated = _normalize_incremental(conn, rows, source_type, source_ids, total)

        total_updated += updated
        conn.commit()

    return total_updated


def _normalize_incremental(conn, rows, source_type: str,
                            source_ids: list[str], total: int) -> int:
    """Update engagement_normalized for a list of (comment_id, like_count) rows."""
    placeholders = ",".join("?" * len(source_ids))
    updated = 0
    for row in rows:
        cid, like_count = row["comment_id"], row["like_count"]
        rank = conn.execute(
            f"SELECT COUNT(*) FROM comments "
            f"WHERE source_type = ? AND channel_id IN ({placeholders}) "
            f"AND like_count <= ?",
            [source_type] + source_ids + [like_count],
        ).fetchone()[0]
        score = round(100.0 * (rank - 1) / max(total - 1, 1), 1)
        conn.execute(
            "UPDATE comments SET engagement_normalized = ? WHERE comment_id = ?",
            (score, cid),
        )
        updated += 1
    return updated


def _normalize_bulk(conn, source_type: str, source_ids: list[str]) -> int:
    """Recompute engagement_normalized for ALL comments of a source_type in one pass."""
    placeholders = ",".join("?" * len(source_ids))
    # Fetch all like_counts sorted
    all_rows = conn.execute(
        f"SELECT comment_id, like_count FROM comments "
        f"WHERE source_type = ? AND channel_id IN ({placeholders}) "
        f"ORDER BY like_count ASC",
        [source_type] + source_ids,
    ).fetchall()

    total = len(all_rows)
    if total == 0:
        return 0

    updates = []
    for i, row in enumerate(all_rows):
        score = round(100.0 * i / max(total - 1, 1), 1)
        updates.append((score, row["comment_id"]))

    conn.executemany(
        "UPDATE comments SET engagement_normalized = ? WHERE comment_id = ?",
        updates,
    )
    return total
