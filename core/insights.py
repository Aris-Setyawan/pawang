"""Insights Engine — usage analytics and reports."""

import time
from core.database import get_db
from core.logger import log


def generate_insights(hours: int = 168) -> str:
    """Generate a comprehensive usage report.

    Args:
        hours: Lookback period (default 7 days = 168h)

    Returns formatted report string.
    """
    db = get_db()
    since = time.time() - (hours * 3600)

    # Overall stats
    total = db.get_total_stats()
    msgs = total.get("messages", {})
    usg = total.get("usage", {})

    # Per-provider stats
    provider_stats = db.get_usage_stats(hours)

    # Per-agent message counts
    agent_rows = db.conn.execute(
        "SELECT agent_id, COUNT(*) as count, COUNT(DISTINCT user_id) as users "
        "FROM messages WHERE created_at > ? GROUP BY agent_id ORDER BY count DESC",
        (since,),
    ).fetchall()

    # Busiest hours
    hour_rows = db.conn.execute(
        "SELECT CAST((created_at % 86400) / 3600 AS INTEGER) as hour, COUNT(*) as count "
        "FROM messages WHERE created_at > ? GROUP BY hour ORDER BY count DESC LIMIT 5",
        (since,),
    ).fetchall()

    # Top users
    user_rows = db.conn.execute(
        "SELECT user_id, COUNT(*) as count FROM messages "
        "WHERE created_at > ? AND role = 'user' GROUP BY user_id ORDER BY count DESC LIMIT 5",
        (since,),
    ).fetchall()

    # Error rate
    error_row = db.conn.execute(
        "SELECT COUNT(*) as total, SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as errors "
        "FROM usage WHERE created_at > ?",
        (since,),
    ).fetchone()

    # Build report
    lines = [f"Pawang Insights ({hours}h)\n{'=' * 35}\n"]

    lines.append(f"Total Messages: {msgs.get('total_messages', 0)}")
    lines.append(f"Total Sessions: {msgs.get('total_sessions', 0)}")
    lines.append(f"API Calls: {usg.get('total_requests', 0)}")
    total_input = usg.get('total_input') or 0
    total_output = usg.get('total_output') or 0
    lines.append(f"Tokens: {total_input:,} in / {total_output:,} out\n")

    if provider_stats:
        lines.append("Provider Usage:")
        for s in provider_stats:
            avg_lat = s.get('avg_latency') or 0
            errors = s.get('errors', 0)
            lines.append(
                f"  {s['provider']}/{s['model']}: "
                f"{s['requests']} req, {avg_lat:.0f}ms avg"
                + (f", {errors} errors" if errors else "")
            )
        lines.append("")

    if agent_rows:
        lines.append("Agent Activity:")
        for r in agent_rows:
            lines.append(f"  {r['agent_id']}: {r['count']} msgs, {r['users']} users")
        lines.append("")

    if hour_rows:
        lines.append("Busiest Hours (UTC):")
        for r in hour_rows:
            lines.append(f"  {r['hour']:02d}:00 — {r['count']} messages")
        lines.append("")

    if user_rows:
        lines.append("Top Users:")
        for r in user_rows:
            lines.append(f"  {r['user_id']}: {r['count']} messages")
        lines.append("")

    if error_row:
        total_req = error_row['total'] or 0
        errors = error_row['errors'] or 0
        rate = (errors / total_req * 100) if total_req > 0 else 0
        lines.append(f"Error Rate: {errors}/{total_req} ({rate:.1f}%)")

    return "\n".join(lines)
