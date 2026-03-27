"""
api/main.py
-----------
FastAPI application — REST endpoints + SSE streaming for pipeline progress.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agents.orchestrator import run_pipeline
from models.financial import EquityBrief, FinancialModel, PipelineState

logger = structlog.get_logger(__name__)

# In-memory cache: ticker → PipelineState
_cache: dict[str, PipelineState] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("finsight_api_startup")
    yield
    logger.info("finsight_api_shutdown")


app = FastAPI(
    title="FinSight AI API",
    version="1.0.0",
    description="Corporate Finance Autopilot — Assiduous Hackathon",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://frontend:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PipelineRequest(BaseModel):
    ticker: str


class PipelineResponse(BaseModel):
    ticker: str
    status: str
    logs: list[str]
    errors: list[str]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/api/pipeline/run", response_model=PipelineResponse)
async def run_pipeline_endpoint(request: PipelineRequest):
    """
    Trigger the full ingest → transform → validate → model → report pipeline.
    Results are cached per ticker.
    """
    ticker = request.ticker.upper()
    logger.info("pipeline_request", ticker=ticker)

    state = await run_pipeline(ticker)
    _cache[ticker] = state

    return PipelineResponse(
        ticker=state.ticker,
        status=state.status,
        logs=state.logs,
        errors=state.errors,
    )


@app.get("/api/pipeline/stream/{ticker}")
async def stream_pipeline(ticker: str):
    """
    SSE endpoint — streams pipeline log lines as they are produced.
    Frontend subscribes to this for real-time progress display.
    """
    ticker = ticker.upper()

    async def event_generator():
        logs_sent = 0
        # Run pipeline in background, poll logs
        task = asyncio.create_task(_run_and_store(ticker))

        while not task.done():
            state = _cache.get(ticker)
            if state:
                new_logs = state.logs[logs_sent:]
                for log in new_logs:
                    yield f"data: {json.dumps({'log': log})}\n\n"
                    logs_sent += len(new_logs)
            await asyncio.sleep(0.5)

        state = _cache.get(ticker)
        if state:
            # Send any remaining logs
            for log in state.logs[logs_sent:]:
                yield f"data: {json.dumps({'log': log})}\n\n"
            yield f"data: {json.dumps({'status': state.status, 'done': True})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _run_and_store(ticker: str):
    state = await run_pipeline(ticker)
    _cache[ticker] = state


@app.get("/api/model/{ticker}", response_model=FinancialModel)
async def get_model(ticker: str):
    """Return the financial model for a ticker (must run pipeline first)."""
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.financial_model:
        raise HTTPException(status_code=404, detail=f"No model found for {ticker}. Run pipeline first.")
    return state.financial_model


@app.get("/api/report/{ticker}", response_model=EquityBrief)
async def get_report(ticker: str):
    """Return the equity brief for a ticker."""
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.equity_brief:
        raise HTTPException(status_code=404, detail=f"No report found for {ticker}. Run pipeline first.")
    return state.equity_brief


@app.get("/api/financials/{ticker}")
async def get_financials(ticker: str):
    """Return raw normalised financials for charting."""
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.raw_financials:
        raise HTTPException(status_code=404, detail=f"No data found for {ticker}.")
    f = state.raw_financials
    return {
        "ticker": f.ticker,
        "profile": f.profile.model_dump(),
        "market": f.market.model_dump(),
        "income_statements": [s.model_dump() for s in f.income_statements],
        "metrics": [m.model_dump() for m in f.metrics],
        "cash_flows": [c.model_dump() for c in f.cash_flows],
    }


@app.get("/api/cache")
async def list_cache():
    """List all tickers currently cached."""
    return {"cached_tickers": list(_cache.keys())}