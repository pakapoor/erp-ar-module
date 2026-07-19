import asyncio
from datetime import date

from sqlalchemy import text

from src.database import engine


TENANT_ID = "00000000-0000-0000-0000-000000000001"
ENTITY_ID = "00000000-0000-0000-0000-000000000002"
USER_RAHUL = "00000000-0000-0000-0000-000000000003"
USER_PRIYA = "00000000-0000-0000-0000-000000000004"
CUSTOMER_TATA = "00000000-0000-0000-0000-000000000005"
GL_CASH = "00000000-0000-0000-0000-000000000010"
GL_AR = "00000000-0000-0000-0000-000000000011"
GL_TAX = "00000000-0000-0000-0000-000000000012"
GL_REVENUE = "00000000-0000-0000-0000-000000000013"
GL_FX = "00000000-0000-0000-0000-000000000014"
PERIOD_ID = "00000000-0000-0000-0000-000000000020"


async def seed_data() -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO tenant (id, name, base_currency)
                VALUES (:id, :name, :base_currency)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": TENANT_ID,
                "name": "Reliance",
                "base_currency": "INR",
            },
        )

        await connection.execute(
            text("""
                INSERT INTO entity (id, tenant_id, name, currency)
                VALUES (:id, :tenant_id, :name, :currency)
                ON CONFLICT DO NOTHING
            """),
            {
                "id": ENTITY_ID,
                "tenant_id": TENANT_ID,
                "name": "Reliance Retail",
                "currency": "INR",
            },
        )

        await connection.execute(
            text("""
                INSERT INTO app_user (
                    id, tenant_id, entity_id, name, email, roles
                )
                VALUES (
                    :id, :tenant_id, :entity_id, :name, :email, :roles
                )
                ON CONFLICT DO NOTHING
            """),
            [
                {
                    "id": USER_RAHUL,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "name": "Rahul",
                    "email": "rahul@reliance.com",
                    "roles": ["invoice_creator"],
                },
                {
                    "id": USER_PRIYA,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "name": "Priya",
                    "email": "priya@reliance.com",
                    "roles": ["invoice_approver", "cfo"],
                },
            ],
        )

        await connection.execute(
            text("""
                INSERT INTO customer (
                    id, tenant_id, entity_id, name, currency,
                    payment_terms, credit_limit
                )
                VALUES (
                    :id, :tenant_id, :entity_id, :name, :currency,
                    :payment_terms, :credit_limit
                )
                ON CONFLICT DO NOTHING
            """),
            {
                "id": CUSTOMER_TATA,
                "tenant_id": TENANT_ID,
                "entity_id": ENTITY_ID,
                "name": "Tata Steel",
                "currency": "INR",
                "payment_terms": "NET30",
                "credit_limit": 10_000_000,
            },
        )

        await connection.execute(
            text("""
                INSERT INTO gl_account (
                    id, tenant_id, entity_id,
                    account_code, account_name, account_type
                )
                VALUES (
                    :id, :tenant_id, :entity_id,
                    :account_code, :account_name, :account_type
                )
                ON CONFLICT DO NOTHING
            """),
            [
                {
                    "id": GL_CASH,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "account_code": "1100",
                    "account_name": "Cash",
                    "account_type": "ASSET",
                },
                {
                    "id": GL_AR,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "account_code": "1200",
                    "account_name": "Accounts Receivable",
                    "account_type": "ASSET",
                },
                {
                    "id": GL_TAX,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "account_code": "2200",
                    "account_name": "Tax Payable",
                    "account_type": "LIABILITY",
                },
                {
                    "id": GL_REVENUE,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "account_code": "3100",
                    "account_name": "Sales Revenue",
                    "account_type": "REVENUE",
                },
                {
                    "id": GL_FX,
                    "tenant_id": TENANT_ID,
                    "entity_id": ENTITY_ID,
                    "account_code": "4300",
                    "account_name": "FX Gain/Loss",
                    "account_type": "EXPENSE",
                },
            ],
        )

        await connection.execute(
            text("""
                INSERT INTO accounting_period (
                    id, tenant_id, entity_id, period_name,
                    start_date, end_date, status
                )
                VALUES (
                    :id, :tenant_id, :entity_id, :period_name,
                    :start_date, :end_date, :status
                )
                ON CONFLICT DO NOTHING
            """),
            {
                "id": PERIOD_ID,
                "tenant_id": TENANT_ID,
                "entity_id": ENTITY_ID,
                "period_name": "July 2026",
                "start_date": date(2026, 7, 1),
                "end_date": date(2026, 7, 31),
                "status": "OPEN",
            },
        )

    await engine.dispose()
    print("Seed data inserted successfully")


if __name__ == "__main__":
    asyncio.run(seed_data())
