#!/usr/bin/env python3
"""
Debug session setup.
Creates a fresh isolated tenant, entity, users, GL accounts, accounting period,
and customer in the DB. Writes session info to debug/.debug_session as JSON.
Run this before any debug invoice/payment scripts.
"""
import asyncio
import json
import uuid
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
import sys
sys.path.insert(0, "/app")
from src.database import engine

SESSION_FILE = Path("/tmp/.debug_session")


def new_id() -> str:
    return str(uuid.uuid4())


async def setup() -> dict:
    tenant_id   = new_id()
    entity_id   = new_id()
    user_creator_id  = new_id()
    user_approver_id = new_id()
    user_payer_id    = new_id()
    customer_id = new_id()
    today = date.today()
    period_start = today.replace(day=1)
    # last day of month
    if today.month == 12:
        period_end = today.replace(month=12, day=31)
    else:
        import calendar
        last_day = calendar.monthrange(today.year, today.month)[1]
        period_end = today.replace(day=last_day)
    period_id = new_id()

    async with engine.begin() as conn:
        # Tenant
        await conn.execute(text("""
            INSERT INTO tenant (id, name, base_currency, is_active, created_at, updated_at)
            VALUES (:id, :name, 'INR', true, NOW(), NOW())
        """), {"id": tenant_id, "name": f"Debug Corp {tenant_id[:8]}"})

        # Entity
        await conn.execute(text("""
            INSERT INTO entity (id, tenant_id, name, currency, is_active, created_at)
            VALUES (:id, :tenant_id, 'Debug Entity', 'INR', true, NOW())
        """), {"id": entity_id, "tenant_id": tenant_id})

        # Users
        await conn.execute(text("""
            INSERT INTO app_user (id, tenant_id, entity_id, name, email, roles, is_active, created_at)
            VALUES
              (:creator_id,  :tid, :eid, 'Debug Creator',  'creator@debug.com',  ARRAY['invoice_creator'],                       true, NOW()),
              (:approver_id, :tid, :eid, 'Debug Approver', 'approver@debug.com', ARRAY['invoice_approver','cfo'],                 true, NOW()),
              (:payer_id,    :tid, :eid, 'Debug Payer',    'payer@debug.com',    ARRAY['payment_recorder','cfo'],                 true, NOW())
        """), {
            "creator_id":  user_creator_id,
            "approver_id": user_approver_id,
            "payer_id":    user_payer_id,
            "tid": tenant_id,
            "eid": entity_id,
        })

        # GL Accounts
        gl_accounts = [
            (new_id(), "1100", "Cash",                "ASSET"),
            (new_id(), "1200", "Accounts Receivable", "ASSET"),
            (new_id(), "2100", "Customer Credit",     "LIABILITY"),
            (new_id(), "2200", "Tax Payable",         "LIABILITY"),
            (new_id(), "3100", "Sales Revenue",       "REVENUE"),
            (new_id(), "4100", "Bad Debt Expense",    "EXPENSE"),
            (new_id(), "4300", "FX Gain/Loss",        "EXPENSE"),
        ]
        for gl_id, code, name, acct_type in gl_accounts:
            await conn.execute(text("""
                INSERT INTO gl_account (id, tenant_id, entity_id, account_code, account_name, account_type, is_active, created_at)
                VALUES (:id, :tid, :eid, :code, :name, :type, true, NOW())
            """), {"id": gl_id, "tid": tenant_id, "eid": entity_id,
                    "code": code, "name": name, "type": acct_type})

        # Accounting period (current month, OPEN)
        await conn.execute(text("""
            INSERT INTO accounting_period (id, tenant_id, entity_id, period_name, start_date, end_date, status, created_at)
            VALUES (:id, :tid, :eid, :name, :start, :end, 'OPEN', NOW())
        """), {
            "id": period_id, "tid": tenant_id, "eid": entity_id,
            "name": today.strftime("%B %Y"),
            "start": period_start, "end": period_end,
        })

        # Customer
        await conn.execute(text("""
            INSERT INTO customer (id, tenant_id, entity_id, name, email, currency, payment_terms, credit_limit, is_active, created_at)
            VALUES (:id, :tid, :eid, 'Debug Customer', 'customer@debug.com', 'INR', 'NET30', 9999999, true, NOW())
        """), {"id": customer_id, "tid": tenant_id, "eid": entity_id})

    await engine.dispose()

    session = {
        "tenant_id":        tenant_id,
        "entity_id":        entity_id,
        "user_creator_id":  user_creator_id,
        "user_approver_id": user_approver_id,
        "user_payer_id":    user_payer_id,
        "customer_id":      customer_id,
        "created_at":       datetime.utcnow().isoformat(),
    }

    SESSION_FILE.write_text(json.dumps(session, indent=2))
    print(json.dumps(session, indent=2))
    print(f"\nSession saved to {SESSION_FILE}")
    return session


if __name__ == "__main__":
    asyncio.run(setup())
