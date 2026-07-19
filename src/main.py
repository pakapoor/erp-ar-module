import uuid
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.database import engine, Base
from src.routers import invoices, payments, aging, journal_entries, health

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# Lifespan — startup and shutdown
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ERP AR Module...")
    # Tables created by migrations — just verify connection
    async with engine.begin() as conn:
        logger.info("Database connection verified ✅")
    yield
    logger.info("Shutting down ERP AR Module...")


# ============================================================
# App
# ============================================================
app = FastAPI(
    title="ERP AR Module",
    description="Multi-tenant Invoicing and Accounts Receivable",
    version="1.0.0",
    lifespan=lifespan,
)


# ============================================================
# CORS middleware
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# Trace ID middleware
# Every request gets a unique X-Trace-ID injected
# Flows through to DB and audit log
# ============================================================
@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-ID", str(uuid.uuid4()))
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-ID"] = trace_id
    return response


# ============================================================
# Exception handlers
# ============================================================
class PeriodClosedException(Exception):
    def __init__(self, message: str):
        self.message = message


class IdempotencyConflictException(Exception):
    def __init__(self, message: str, status: str):
        self.message = message
        self.status = status


class BusinessRuleException(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message


class VersionConflictException(Exception):
    def __init__(self, message: str):
        self.message = message


@app.exception_handler(PeriodClosedException)
async def period_closed_handler(request: Request, exc: PeriodClosedException):
    return JSONResponse(
        status_code=status.HTTP_423_LOCKED,
        content={
            "error": {
                "code": "PERIOD_CLOSED",
                "message": exc.message,
                "request_id": getattr(request.state, "trace_id", None),
            }
        },
    )


@app.exception_handler(IdempotencyConflictException)
async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictException):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "code": exc.status,
                "message": exc.message,
                "request_id": getattr(request.state, "trace_id", None),
            }
        },
    )


@app.exception_handler(BusinessRuleException)
async def business_rule_handler(request: Request, exc: BusinessRuleException):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": getattr(request.state, "trace_id", None),
            }
        },
    )


@app.exception_handler(VersionConflictException)
async def version_conflict_handler(request: Request, exc: VersionConflictException):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": {
                "code": "VERSION_CONFLICT",
                "message": exc.message,
                "request_id": getattr(request.state, "trace_id", None),
            }
        },
    )


# ============================================================
# Routers
# ============================================================
app.include_router(health.router, tags=["Health"])
app.include_router(invoices.router, prefix="/api/v1", tags=["Invoices"])
app.include_router(payments.router, prefix="/api/v1", tags=["Payments"])
app.include_router(aging.router, prefix="/api/v1", tags=["AR Aging"])
app.include_router(journal_entries.router, prefix="/api/v1", tags=["Journal Entries"])
