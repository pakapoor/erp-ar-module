from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db

import logging
logger = logging.getLogger(__name__)

# ============================================================
# HEALTH ROUTER
# ============================================================
router = APIRouter()

APP_VERSION = "1.0.0"


@router.get("/api/v1/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    GET /api/v1/health
    NFR5 — Observability

    No auth required — used by Docker, load balancer, DataDog.
    Checks:
    - Database connectivity
    - AR aging MV freshness
    - Last reconciliation status
    """
    checks = {}
    overall = "healthy"

    # ── DB connectivity ────────────────────────────────────
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "healthy"
    except Exception as e:
        checks["database"] = f"unhealthy: {str(e)}"
        overall = "unhealthy"
        logger.error(f"Health check DB failed: {e}")

    # ── AR aging MV freshness ──────────────────────────────
    try:
        result = await db.execute(
            text("SELECT MAX(as_of) as last_refresh FROM ar_aging")
        )
        row = result.fetchone()
        if row and row.last_refresh:
            age_minutes = (
                datetime.utcnow() - row.last_refresh
            ).total_seconds() / 60
            checks["materialized_view_age_minutes"] = round(age_minutes, 1)
            if age_minutes > 10:
                checks["materialized_view_status"] = "stale"
                overall = "degraded"
            else:
                checks["materialized_view_status"] = "healthy"
        else:
            checks["materialized_view_status"] = "not_populated"
    except Exception as e:
        checks["materialized_view_status"] = f"unknown: {str(e)}"

    # ── Reconciliation status ──────────────────────────────
    # Check AR subledger = GL account 1200 balance
    try:
        result = await db.execute(
            text("""
                SELECT
                    (SELECT COALESCE(SUM(balance_amount), 0)
                     FROM invoice
                     WHERE status NOT IN ('PAID', 'VOID', 'WRITTEN_OFF')
                    ) as subledger_balance,
                    (SELECT COALESCE(SUM(jel.debit_amount) - SUM(jel.credit_amount), 0)
                     FROM journal_entry_line jel
                     JOIN gl_account ga ON ga.id = jel.gl_account_id
                     WHERE ga.account_code = '1200'
                    ) as gl_balance
            """)
        )
        row = result.fetchone()
        if row:
            diff = abs(float(row.subledger_balance) - float(row.gl_balance))
            if diff < 0.01:
                checks["reconciliation_status"] = "MATCHED"
                checks["last_reconciliation"] = datetime.utcnow().isoformat()
            else:
                checks["reconciliation_status"] = "MISMATCH"
                checks["reconciliation_diff"] = round(diff, 4)
                overall = "unhealthy"
                logger.error(
                    f"RECONCILIATION MISMATCH DETECTED! "
                    f"Subledger: {row.subledger_balance} "
                    f"GL: {row.gl_balance} "
                    f"Diff: {diff}"
                )
    except Exception as e:
        checks["reconciliation_status"] = f"unknown: {str(e)}"

    status_code = 200 if overall == "healthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": APP_VERSION,
            "timestamp": datetime.utcnow().isoformat(),
            "checks": checks,
        }
    )
