"""Scheduled Jobs — balance alerts, health reports, DB cleanup."""

import asyncio
import os
import time

from core.logger import log
from core.database import get_db


SCRIPTS_DIR = "/root/pawang/scripts"


async def balance_alert(scheduler):
    """Check balances and alert if any provider is low."""
    script = os.path.join(SCRIPTS_DIR, "check-balances.sh")
    if not os.path.exists(script):
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode(errors="replace")

        # Parse DeepSeek balance for low-balance alert
        alerts = []
        for line in output.split("\n"):
            line_stripped = line.strip()
            if line_stripped.startswith("Balance: $"):
                try:
                    bal = float(line_stripped.replace("Balance: $", ""))
                    if bal < 1.0:
                        alerts.append(f"⚠️ DeepSeek balance rendah: ${bal:.2f}")
                except ValueError:
                    pass

        if alerts:
            msg = "🔔 Balance Alert\n" + "\n".join(alerts)
            await scheduler.notify(msg)
            log.warning(f"Balance alert: {alerts}")

    except asyncio.TimeoutError:
        log.warning("Balance check timeout")
    except Exception as e:
        log.error(f"Balance alert error: {e}")


async def health_report(scheduler):
    """Generate health summary and alert on issues."""
    from main import health_monitor
    if not health_monitor:
        return

    await health_monitor.check_all()
    status = health_monitor.get_all_status()

    unhealthy = []
    for name, s in status.items():
        if not s.healthy:
            unhealthy.append(f"  ❌ {name}: {s.last_error or 'down'}")
        elif s.rate_limited:
            unhealthy.append(f"  ⚠️ {name}: rate limited")
        elif not s.auth_valid:
            unhealthy.append(f"  🔑 {name}: invalid key")

    if unhealthy:
        msg = "🏥 Health Alert\n" + "\n".join(unhealthy)
        await scheduler.notify(msg)
        log.warning(f"Health alert: {len(unhealthy)} issues")


async def db_cleanup(scheduler):
    """Clean old messages and usage records (>30 days)."""
    db = get_db()
    cutoff = time.time() - (30 * 86400)  # 30 days

    try:
        cur = db.conn.execute(
            "DELETE FROM messages WHERE created_at < ?", (cutoff,)
        )
        msg_deleted = cur.rowcount

        cur = db.conn.execute(
            "DELETE FROM usage WHERE created_at < ?", (cutoff,)
        )
        usage_deleted = cur.rowcount

        db.conn.commit()

        if msg_deleted > 0 or usage_deleted > 0:
            log.info(f"DB cleanup: {msg_deleted} messages, {usage_deleted} usage records removed")
    except Exception as e:
        log.error(f"DB cleanup error: {e}")


async def learn_from_history(scheduler):
    """Batch extract knowledge from recent conversations."""
    try:
        from core.learning import batch_learn_from_history
        batch_learn_from_history(hours=6)
    except Exception as e:
        log.error(f"Learning job error: {e}")


async def cache_and_knowledge_cleanup(scheduler):
    """Clean expired cache entries and decay old knowledge."""
    try:
        from core.response_cache import get_response_cache
        get_response_cache().cleanup_expired()
    except Exception as e:
        log.warning(f"Cache cleanup error: {e}")

    try:
        from core.knowledge import get_knowledge_base
        kb = get_knowledge_base()
        kb.decay_old(days=30)
        kb.cleanup(max_entries=10000)
    except Exception as e:
        log.warning(f"Knowledge cleanup error: {e}")


def register_jobs(scheduler, config=None):
    """Register all scheduled jobs. Intervals from config if provided."""
    from core.config import get_config
    if config is None:
        config = get_config()

    sched_cfg = config.scheduler

    async def _balance_alert():
        await balance_alert(scheduler)

    async def _health_report():
        await health_report(scheduler)

    async def _db_cleanup():
        await db_cleanup(scheduler)

    async def _learn():
        await learn_from_history(scheduler)

    async def _cache_cleanup():
        await cache_and_knowledge_cleanup(scheduler)

    scheduler.add_job("balance_alert", interval=sched_cfg.balance_alert_interval,
                       func=_balance_alert, enabled=sched_cfg.enabled)

    scheduler.add_job("health_report", interval=sched_cfg.health_report_interval,
                       func=_health_report, enabled=sched_cfg.enabled)

    scheduler.add_job("db_cleanup", interval=sched_cfg.db_cleanup_interval,
                       func=_db_cleanup, enabled=sched_cfg.enabled)

    # Learning & cache jobs
    scheduler.add_job("learn_from_history", interval=21600,
                       func=_learn, enabled=sched_cfg.enabled)

    scheduler.add_job("cache_cleanup", interval=86400,
                       func=_cache_cleanup, enabled=sched_cfg.enabled)
