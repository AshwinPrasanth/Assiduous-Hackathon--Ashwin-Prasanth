"""
api/main.py
-----------
FastAPI application.

Key design: /api/pipeline/run returns ALL results (logs + report + model)
in a single response, avoiding the cache-wipe problem with --reload.
The /api/report and /api/model endpoints are kept for compatibility but
the frontend primarily uses the all-in-one response.
"""

from __future__ import annotations
import os
from contextlib import asynccontextmanager
import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agents.orchestrator import run_pipeline
from models.financial import EquityBrief, FinancialModel, PipelineState
from agents.financial_model_agent import build_sensitivity_table

logger = structlog.get_logger(__name__)

_cache: dict[str, PipelineState] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("finsight_api_startup")
    yield
    logger.info("finsight_api_shutdown")


app = FastAPI(title="FinSight AI API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PipelineRequest(BaseModel):
    ticker: str


def _norm(s) -> str:
    """Normalize Pydantic enum key to plain lowercase string."""
    raw = str(s)
    if "." in raw:
        raw = raw.split(".")[-1]
    return raw.split(":")[0].strip(" <>'\"").lower()


def _serialize_model(fm: FinancialModel) -> dict:
    """Serialize FinancialModel with guaranteed string scenario keys."""
    def serialize_dcf(dcf) -> dict:
        d = dcf.model_dump()
        d["scenario"] = _norm(d.get("scenario", ""))
        if "assumptions" in d:
            d["assumptions"]["scenario"] = _norm(d["assumptions"].get("scenario", ""))
        return d

    return {
        "ticker": fm.ticker,
        "base_year_revenue": fm.base_year_revenue,
        "base_year_fcf": fm.base_year_fcf,
        "shares_outstanding": fm.shares_outstanding,
        "net_debt": fm.net_debt,
        "scenarios": {
            _norm(scenario): serialize_dcf(dcf)
            for scenario, dcf in fm.scenarios.items()
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


@app.post("/api/pipeline/run")
async def run_pipeline_endpoint(request: PipelineRequest):
    """
    Run the full pipeline and return everything in one response:
    logs, status, report, and model. This avoids any cache-invalidation
    issues from uvicorn --reload wiping in-memory state between requests.
    """
    ticker = request.ticker.upper()
    logger.info("PIPELINE_START", ticker=ticker)
    try:
        state = await run_pipeline(ticker)
        _cache[ticker] = state  # still cache for /api/report and /api/model

        sensitivity = None
        if state.financial_model:
            try:
                sensitivity = build_sensitivity_table(state.financial_model)
            except Exception as e:
                logger.warning("sensitivity_build_failed", error=str(e))

        response = {
            "ticker": state.ticker,
            "status": state.status,
            "logs": state.logs,
            "errors": state.errors,
            "report": state.equity_brief.model_dump() if state.equity_brief else None,
            "model": _serialize_model(state.financial_model) if state.financial_model else None,
            "sensitivity": sensitivity,
        }

        logger.info(
            "pipeline_response_built",
            ticker=ticker,
            has_report=response["report"] is not None,
            has_model=response["model"] is not None,
            scenario_keys=list(response["model"]["scenarios"].keys()) if response["model"] else [],
        )
        return JSONResponse(content=response)

    except Exception as e:
        logger.error("PIPELINE_ERROR", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/report/{ticker}", response_model=EquityBrief)
async def get_report(ticker: str):
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.equity_brief:
        raise HTTPException(status_code=404, detail="Run pipeline first.")
    return state.equity_brief


@app.get("/api/model/{ticker}")
async def get_model(ticker: str):
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.financial_model:
        raise HTTPException(status_code=404, detail="Run pipeline first.")
    return JSONResponse(content=_serialize_model(state.financial_model))


@app.get("/api/sensitivity/{ticker}")
async def get_sensitivity(ticker: str):
    """
    Return a WACC × terminal growth rate sensitivity grid for the base DCF.
    Used to render the sensitivity table in the DCF Model tab.
    """
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.financial_model:
        raise HTTPException(status_code=404, detail="Run pipeline first.")
    table = build_sensitivity_table(state.financial_model)
    return JSONResponse(content=table)


@app.get("/api/cache")
async def list_cache():
    return {"cached_tickers": list(_cache.keys())}