#!/usr/bin/env python3
"""
Seed users, GL accounts, an OPEN accounting period, and a customer for an
EXISTING tenant/entity (created beforehand via the real POST /tenants and
POST /entities APIs -- see 0_create_tenant.sh / 0_create_entity.sh).

Invoices still need a customer, chart of accounts, and an open period, none
of which have CRUD APIs yet, so those are seeded directly here. Unlike
setup_debug_session.py (which mints a brand-new tenant/entity every run),
this variant takes tenant_id/entity_id as arguments and only fills in what
that entity is still missing -- safe to rerun against the same entity.

Usage: python setup_debug_session_for_entity.py TENANT_ID ENTITY_ID
Prints JSON to stdout: {creator_id, approver_id, payer_id, customer_id}
"""
import asyncio
import json
import sys
import uuid
from datetime import date

from sqlalchemy import text
sys.path.insert(0, "/app")
from src.database import engine


def new_id() -> str:
    return str(uuid.uuid4())


GL_ACCOUNTS = [
    ("1100", "Cash", "ASSET"),
    ("1200", "Accounts Receivable", "ASSET"),
    ("2100", "Customer Credit", "LIABILITY"),
    ("2200", "Tax Payable", "LIABILITY"),
    ("3100", "Sales Revenue", "REVENUE"),
    ("4100", "Bad Debt Expense", "EXPENSE"),
    ("4300", "FX Gain/Loss", "EXPENSE"),
]


async def setup(tenant_id: str, entity_id: str) -> dict:
    creator_id = new_id()
    approver_id = new_id()
    payer_id = new_id()
    customer_id = new_id()

    entity_result_currency = None

    async with engine.begin() as conn:
        currency_row = await conn.execute(
            text("SELECT currency FROM entity WHERE id = :id AND tenant_id = :tid"),
            {"id": entity_id, "tid": tenant_id},
        )
        entity_result_currency = currency_row.scalar_one()

        await conn.execute(text("""
            INSERT INTO app_user (id, tenant_id, entity_id, name, email, roles, is_active, created_at)
            VALUES
              (:creator_id,  :tid, :eid, 'Debug Creator',  :creator_email,  ARRAY['invoice_creator'],            true, NOW()),
              (:approver_id, :tid, :eid, 'Debug Approver', :approver_email, ARRAY['invoice_approver','cfo'],     true, NOW()),
              (:payer_id,    :tid, :eid, 'Debug Payer',    :payer_email,    ARRAY['payment_recorder','cfo'],     true, NOW())
        """), {
            "creator_id": creator_id, "approver_id": approver_id, "payer_id": payer_id,
            "tid": tenant_id, "eid": entity_id,
            "creator_email": f"creator-{entity_id[:8]}@debug.com",
            "approver_email": f"approver-{entity_id[:8]}@debug.com",
            "payer_email": f"payer-{entity_id[:8]}@debug.com",
        })

        for code, name, acct_type in GL_ACCOUNTS:
            await conn.execute(text("""
                INSERT INTO gl_account (id, tenant_id, entity_id, account_code, account_name, account_type, is_active, created_at)
                VALUES (:id, :tid, :eid, :code, :name, :type, true, NOW())
                ON CONFLICT DO NOTHING
            """), {"id": new_id(), "tid": tenant_id, "eid": entity_id,
                    "code": code, "name": name, "type": acct_type})

        today = date.today()
        period_start = today.replace(day=1)
        if today.month == 12:
            period_end = today.replace(month=12, day=31)
        else:
            import calendar
            last_day = calendar.monthrange(today.year, today.month)[1]
            period_end = today.replace(day=last_day)

        await conn.execute(text("""
            INSERT INTO accounting_period (id, tenant_id, entity_id, period_name, start_date, end_date, status, created_at)
            VALUES (:id, :tid, :eid, :name, :start, :end, 'OPEN', NOW())
            ON CONFLICT DO NOTHING
        """), {
            "id": new_id(), "tid": tenant_id, "eid": entity_id,
            "name": today.strftime("%B %Y"),
            "start": period_start, "end": period_end,
        })

        await conn.execute(text("""
            INSERT INTO customer (id, tenant_id, entity_id, name, email, currency, payment_terms, credit_limit, is_active, created_at)
            VALUES (:id, :tid, :eid, 'Debug Customer', :email, :currency, 'NET30', 9999999, true, NOW())
        """), {
            "id": customer_id, "tid": tenant_id, "eid": entity_id,
            "email": f"customer-{entity_id[:8]}@debug.com",
            "currency": entity_result_currency,
        })

    await engine.dispose()

    result = {
        "creator_id": creator_id,
        "approver_id": approver_id,
        "payer_id": payer_id,
        "customer_id": customer_id,
    }
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python setup_debug_session_for_entity.py TENANT_ID ENTITY_ID", file=sys.stderr)
        sys.exit(1)
    asyncio.run(setup(sys.argv[1], sys.argv[2]))
