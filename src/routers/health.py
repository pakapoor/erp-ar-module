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


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    GET /health
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
            text("""
                SELECT EXTRACT(
                    EPOCH FROM (NOW() - MAX(as_of))
                ) / 60 AS age_minutes
                FROM ar_aging
            """)
        )
        row = result.fetchone()
        if row and row.age_minutes is not None:
            age_minutes = float(row.age_minutes)
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
    # Check posted AR subledger = base-currency GL account 1200 balance for
    # every tenant/entity. DRAFT invoices have not posted to the GL.
    try:
        result = await db.execute(
            text("""
                WITH entity_keys AS (
                    SELECT tenant_id, entity_id FROM invoice
                    UNION
                    SELECT tenant_id, entity_id FROM gl_account
                ),
                subledger AS (
                    SELECT tenant_id, entity_id,
                           COALESCE(SUM(base_balance_amount), 0) AS balance
                    FROM invoice
                    WHERE status IN ('APPROVED', 'SENT', 'PARTIALLY_PAID')
                    GROUP BY tenant_id, entity_id
                ),
                general_ledger AS (
                    SELECT ga.tenant_id, ga.entity_id,
                           COALESCE(
                               SUM(jel.base_debit_amount)
                               - SUM(jel.base_credit_amount),
                               0
                           ) AS balance
                    FROM journal_entry_line jel
                    JOIN gl_account ga ON ga.id = jel.gl_account_id
                    WHERE ga.account_code = '1200'
                    GROUP BY ga.tenant_id, ga.entity_id
                )
                SELECT
                    COALESCE(SUM(COALESCE(s.balance, 0)), 0) AS subledger_balance,
                    COALESCE(SUM(COALESCE(g.balance, 0)), 0) AS gl_balance,
                    COALESCE(
                        MAX(ABS(COALESCE(s.balance, 0) - COALESCE(g.balance, 0))),
                        0
                    ) AS max_entity_diff,
                    COUNT(*) FILTER (
                        WHERE ABS(COALESCE(s.balance, 0) - COALESCE(g.balance, 0)) >= 0.01
                    ) AS mismatched_entities
                FROM entity_keys k
                LEFT JOIN subledger s
                  ON s.tenant_id = k.tenant_id AND s.entity_id = k.entity_id
                LEFT JOIN general_ledger g
                  ON g.tenant_id = k.tenant_id AND g.entity_id = k.entity_id
            """)
        )
        row = result.fetchone()
        if row:
            diff = float(row.max_entity_diff)
            if row.mismatched_entities == 0:
                checks["reconciliation_status"] = "MATCHED"
                checks["last_reconciliation"] = datetime.utcnow().isoformat()
            else:
                checks["reconciliation_status"] = "MISMATCH"
                checks["reconciliation_diff"] = round(diff, 4)
                checks["mismatched_entities"] = row.mismatched_entities
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
