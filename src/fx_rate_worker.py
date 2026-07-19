import argparse
import asyncio
import csv
import hashlib
import io
import logging
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Optional

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from src.database import AsyncSessionLocal
from src.models import ExchangeRate, FXImportJob, Tenant


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ECB_API_URL = os.getenv(
    "FX_PROVIDER_URL",
    "https://data-api.ecb.europa.eu/service/data/EXR/"
    "D.CAD+CHF+CNY+GBP+INR+JPY+USD.EUR.SP00.A",
)
SUPPORTED_CURRENCIES = tuple(
    code.strip().upper()
    for code in os.getenv(
        "FX_SUPPORTED_CURRENCIES",
        "INR,USD,EUR,CNY,GBP,JPY,CHF,CAD",
    ).split(",")
    if code.strip()
)
PROVIDER = os.getenv("FX_PROVIDER", "ECB")
BASE_CURRENCY = os.getenv("FX_BASE_CURRENCY", "INR").upper()
POLL_INTERVAL_SECONDS = float(os.getenv("FX_POLL_INTERVAL_SECONDS", "10"))
LEASE_SECONDS = int(os.getenv("FX_JOB_LEASE_SECONDS", "120"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("FX_PROVIDER_TIMEOUT_SECONDS", "15"))
MAX_RATE_AGE_DAYS = int(os.getenv("FX_MAX_RATE_AGE_DAYS", "3"))
ENQUEUE_ON_STARTUP = os.getenv("FX_ENQUEUE_ON_STARTUP", "true").lower() == "true"
RATE_QUANTUM = Decimal("0.00000001")


def utc_now() -> datetime:
    """Return naive UTC to match the schema's TIMESTAMP columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def india_today() -> date:
    return (datetime.now(UTC) + timedelta(hours=5, minutes=30)).date()


def parse_ecb_csv(payload: str) -> tuple[date, dict[str, Decimal]]:
    """Validate one latest-observation ECB CSV batch."""
    reader = csv.DictReader(io.StringIO(payload))
    required_columns = {
        "CURRENCY", "CURRENCY_DENOM", "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS"
    }
    if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
        raise ValueError("ECB response is missing required CSV columns")

    quotes: dict[str, Decimal] = {}
    provider_dates: set[date] = set()
    expected = set(SUPPORTED_CURRENCIES) - {"EUR"}

    for row in reader:
        currency = (row.get("CURRENCY") or "").strip().upper()
        if currency not in expected:
            continue
        if (row.get("CURRENCY_DENOM") or "").strip().upper() != "EUR":
            raise ValueError(f"ECB denominator for {currency} is not EUR")
        if currency in quotes:
            raise ValueError(f"ECB response contains duplicate {currency} quote")
        try:
            provider_date = date.fromisoformat((row.get("TIME_PERIOD") or "").strip())
            value = Decimal((row.get("OBS_VALUE") or "").strip())
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"Invalid ECB quote for {currency}") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError(f"ECB quote for {currency} must be positive and finite")
        if (row.get("OBS_STATUS") or "").strip() not in {"A", ""}:
            raise ValueError(f"ECB quote for {currency} is not available")
        quotes[currency] = value
        provider_dates.add(provider_date)

    missing = sorted(expected - set(quotes))
    if missing:
        raise ValueError(f"ECB response is missing currencies: {', '.join(missing)}")
    if len(provider_dates) != 1:
        raise ValueError("ECB response does not contain one coherent provider date")

    return provider_dates.pop(), quotes


def derive_base_rates(quotes: dict[str, Decimal]) -> dict[str, dict]:
    """Convert ECB's foreign-per-EUR quotes into INR-per-foreign rates."""
    if BASE_CURRENCY != "INR":
        raise ValueError("V1 FX worker supports INR-base tenants only")
    inr_per_eur = quotes.get("INR")
    if inr_per_eur is None:
        raise ValueError("ECB response is missing the INR anchor quote")

    rates: dict[str, dict] = {}
    for currency in SUPPORTED_CURRENCIES:
        if currency == BASE_CURRENCY:
            continue
        if currency == "EUR":
            raw_quote = inr_per_eur
            derived_rate = inr_per_eur
            is_derived = False
        else:
            raw_quote = quotes[currency]
            derived_rate = inr_per_eur / raw_quote
            is_derived = True
        rates[currency] = {
            "rate": derived_rate.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN),
            "raw_quote_rate": raw_quote.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN),
            "is_derived": is_derived,
        }
    return rates


async def enqueue_import(requested_date: Optional[date] = None) -> None:
    requested_date = requested_date or india_today()
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                insert(FXImportJob)
                .values(provider=PROVIDER, requested_date=requested_date)
                .on_conflict_do_nothing(
                    constraint="fx_import_job_provider_date_unique"
                )
            )


async def claim_job() -> Optional[dict]:
    now = utc_now()
    stale_lock = now - timedelta(seconds=LEASE_SECONDS)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            result = await db.execute(
                select(FXImportJob)
                .where(
                    and_(
                        FXImportJob.attempt_count < FXImportJob.max_attempts,
                        or_(
                            and_(
                                FXImportJob.status.in_(["PENDING", "RETRY"]),
                                FXImportJob.next_attempt_at <= now,
                            ),
                            and_(
                                FXImportJob.status == "PROCESSING",
                                FXImportJob.locked_at < stale_lock,
                            ),
                        ),
                    )
                )
                .order_by(FXImportJob.requested_date, FXImportJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if job is None:
                return None
            job.status = "PROCESSING"
            job.attempt_count += 1
            job.locked_at = now
            job.updated_at = now
            await db.flush()
            return {
                "id": job.id,
                "requested_date": job.requested_date,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
            }


async def fetch_ecb_rates(client: httpx.AsyncClient) -> tuple[str, date, dict[str, Decimal]]:
    response = await client.get(
        ECB_API_URL,
        params={"format": "csvdata", "lastNObservations": "1"},
        headers={"Accept": "text/csv", "User-Agent": "erp-ar-fx-worker/1.0"},
    )
    response.raise_for_status()
    provider_date, quotes = parse_ecb_csv(response.text)
    return response.text, provider_date, quotes


async def store_rates(
    job: dict,
    raw_payload: str,
    provider_date: date,
    quotes: dict[str, Decimal],
) -> int:
    age_days = (job["requested_date"] - provider_date).days
    if age_days < 0:
        raise ValueError("ECB provider date is later than the requested date")
    if age_days > MAX_RATE_AGE_DAYS:
        raise ValueError(
            f"ECB rate is {age_days} days old; maximum is {MAX_RATE_AGE_DAYS}"
        )

    payload_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
    derived_rates = derive_base_rates(quotes)
    now = utc_now()

    async with AsyncSessionLocal() as db:
        async with db.begin():
            tenant_result = await db.execute(
                select(Tenant.id).where(
                    and_(Tenant.is_active.is_(True), Tenant.base_currency == BASE_CURRENCY)
                )
            )
            tenant_ids = list(tenant_result.scalars())
            rows = []
            for tenant_id in tenant_ids:
                for currency, values in derived_rates.items():
                    rows.append({
                        "tenant_id": tenant_id,
                        "from_currency": currency,
                        "to_currency": BASE_CURRENCY,
                        "rate": values["rate"],
                        "effective_date": provider_date,
                        "source": PROVIDER,
                        "rate_type": "DAILY_REFERENCE",
                        "status": "APPROVED",
                        "provider_effective_date": provider_date,
                        "fetched_at": now,
                        "raw_quote_currency": "EUR",
                        "raw_quote_rate": values["raw_quote_rate"],
                        "is_derived": values["is_derived"],
                        "raw_response_hash": payload_hash,
                        "import_job_id": job["id"],
                        "is_manual_override": False,
                        "approved_at": now,
                    })
            if rows:
                await db.execute(
                    insert(ExchangeRate).values(rows).on_conflict_do_nothing()
                )
            await db.execute(
                update(FXImportJob)
                .where(
                    and_(
                        FXImportJob.id == job["id"],
                        FXImportJob.status == "PROCESSING",
                    )
                )
                .values(
                    provider_effective_date=provider_date,
                    status="COMPLETED",
                    completed_at=now,
                    raw_response_hash=payload_hash,
                    locked_at=None,
                    last_error=None,
                    updated_at=now,
                )
            )
    return len(rows)


async def mark_failed(job: dict, error: Exception) -> None:
    now = utc_now()
    terminal = job["attempt_count"] >= job["max_attempts"]
    backoff_seconds = min(2 ** job["attempt_count"], 3600)
    async with AsyncSessionLocal() as db:
        async with db.begin():
            await db.execute(
                update(FXImportJob)
                .where(FXImportJob.id == job["id"])
                .values(
                    status="DEAD" if terminal else "RETRY",
                    next_attempt_at=now + timedelta(seconds=backoff_seconds),
                    locked_at=None,
                    last_error=str(error)[:2000],
                    updated_at=now,
                )
            )


async def process_job(client: httpx.AsyncClient, job: dict) -> None:
    raw_payload, provider_date, quotes = await fetch_ecb_rates(client)
    row_count = await store_rates(job, raw_payload, provider_date, quotes)
    logger.info(
        "Completed FX import job %s: provider_date=%s rows=%s",
        job["id"],
        provider_date,
        row_count,
    )


async def run_once(enqueue: bool = False) -> bool:
    if enqueue:
        await enqueue_import()
    job = await claim_job()
    if job is None:
        logger.info("No due FX import job")
        return False
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        try:
            await process_job(client, job)
        except Exception as exc:
            logger.warning(
                "FX import failed for job %s (attempt %s/%s): %s",
                job["id"],
                job["attempt_count"],
                job["max_attempts"],
                exc,
            )
            await mark_failed(job, exc)
            return False
    return True


async def run_worker() -> None:
    logger.info("FX rate worker started; provider=%s", ECB_API_URL)
    if ENQUEUE_ON_STARTUP:
        await enqueue_import()
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        while True:
            job = await claim_job()
            if job is None:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue
            try:
                await process_job(client, job)
            except Exception as exc:
                logger.warning(
                    "FX import failed for job %s (attempt %s/%s): %s",
                    job["id"],
                    job["attempt_count"],
                    job["max_attempts"],
                    exc,
                )
                await mark_failed(job, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="ECB FX rate import worker")
    parser.add_argument("--once", action="store_true", help="process at most one due job")
    parser.add_argument("--enqueue", action="store_true", help="enqueue today's job first")
    args = parser.parse_args()
    if args.once:
        asyncio.run(run_once(enqueue=args.enqueue))
    else:
        asyncio.run(run_worker())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("FX rate worker stopped")
