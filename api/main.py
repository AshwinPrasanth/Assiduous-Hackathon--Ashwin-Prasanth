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
    lifespan=lifespan,
)

# CRITICAL: Allow CORS so the frontend can talk to the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class PipelineRequest(BaseModel):
    ticker: str

class PipelineResponse(BaseModel):
    ticker: str
    status: str
    logs: list[str]
    errors: list[str]

@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}

@app.post("/api/pipeline/run", response_model=PipelineResponse)
async def run_pipeline_endpoint(request: PipelineRequest):
    ticker = request.ticker.upper()
    logger.info("PIPELINE_START", ticker=ticker)

    try:
        # We AWAIT the pipeline here. This keeps the connection alive 
        # while the 70B model works.
        state = await run_pipeline(ticker)
        _cache[ticker] = state

        return PipelineResponse(
            ticker=state.ticker,
            status=state.status,
            logs=state.logs,
            errors=state.errors,
        )
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

@app.get("/api/model/{ticker}", response_model=FinancialModel)
async def get_model(ticker: str):
    ticker = ticker.upper()
    state = _cache.get(ticker)
    if not state or not state.financial_model:
        raise HTTPException(status_code=404, detail="Run pipeline first.")
    return state.financial_model

@app.get("/api/cache")
async def list_cache():
    return {"cached_tickers": list(_cache.keys())}


