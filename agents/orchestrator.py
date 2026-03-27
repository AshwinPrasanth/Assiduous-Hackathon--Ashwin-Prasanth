"""
agents/orchestrator.py
----------------------
LangGraph orchestrator — wires ingest → transform → validate → model → report
into a directed state graph with observable step transitions.

Each node logs its entry/exit and updates PipelineState.logs.
Failed nodes update PipelineState.errors and halt the graph.
"""

from __future__ import annotations

from typing import Any

import structlog
from langgraph.graph import END, StateGraph

from agents.financial_model_agent import run_financial_model
from agents.report_agent import run_report_agent
from models.financial import (
    NormalisedFinancials,
    PipelineState,
)
from pipelines.ingest import (
    ingest_balance_sheets,
    ingest_cash_flows,
    ingest_company_profile,
    ingest_income_statements,
    ingest_market_data,
)
from pipelines.transform import transform
from pipelines.validate import validate

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Node functions (each returns a dict of state updates)
# ---------------------------------------------------------------------------

async def node_ingest(state: dict) -> dict:
    ticker = state["ticker"]
    logs = state.get("logs", [])
    logger.info("node_enter", node="ingest", ticker=ticker)
    logs.append(f"[ingest] Fetching data for {ticker}...")
    try:
        profile = await ingest_company_profile(ticker)
        market = await ingest_market_data(ticker)
        incomes = await ingest_income_statements(ticker)
        balances = await ingest_balance_sheets(ticker)
        cash_flows = await ingest_cash_flows(ticker)

        financials = NormalisedFinancials(
            ticker=ticker,
            profile=profile,
            market=market,
            income_statements=incomes,
            balance_sheets=balances,
            cash_flows=cash_flows,
            metrics=[],
        )
        logs.append(
            f"[ingest] ✓ {len(incomes)} income statements, "
            f"{len(balances)} balance sheets, {len(cash_flows)} cash flows"
        )
        logger.info("node_exit", node="ingest", ticker=ticker, status="ok")
        return {"raw_financials": financials, "logs": logs, "status": "running"}
    except Exception as exc:
        msg = f"[ingest] ✗ {exc}"
        logs.append(msg)
        logger.error("node_error", node="ingest", ticker=ticker, error=str(exc))
        return {"logs": logs, "errors": [msg], "status": "failed"}


async def node_transform(state: dict) -> dict:
    logs = state.get("logs", [])
    financials: NormalisedFinancials = state["raw_financials"]
    logger.info("node_enter", node="transform", ticker=financials.ticker)
    logs.append("[transform] Computing derived metrics...")
    try:
        financials = transform(financials)
        logs.append(f"[transform] ✓ {len(financials.metrics)} metric periods computed")
        logger.info("node_exit", node="transform", status="ok")
        return {"raw_financials": financials, "logs": logs}
    except Exception as exc:
        msg = f"[transform] ✗ {exc}"
        logs.append(msg)
        return {"logs": logs, "errors": [msg], "status": "failed"}


async def node_validate(state: dict) -> dict:
    logs = state.get("logs", [])
    financials: NormalisedFinancials = state["raw_financials"]
    logger.info("node_enter", node="validate", ticker=financials.ticker)
    logs.append("[validate] Running consistency checks...")
    try:
        result = validate(financials)
        for w in result.warnings:
            logs.append(f"[validate] ⚠ {w}")
        if not result.passed:
            for e in result.errors:
                logs.append(f"[validate] ✗ {e}")
            return {"logs": logs, "errors": result.errors, "status": "failed"}
        logs.append(f"[validate] ✓ Passed ({len(result.warnings)} warnings)")
        logger.info("node_exit", node="validate", status="ok")
        return {"logs": logs}
    except Exception as exc:
        msg = f"[validate] ✗ {exc}"
        logs.append(msg)
        return {"logs": logs, "errors": [msg], "status": "failed"}


async def node_model(state: dict) -> dict:
    logs = state.get("logs", [])
    financials: NormalisedFinancials = state["raw_financials"]
    logger.info("node_enter", node="model", ticker=financials.ticker)
    logs.append("[model] Building 3-scenario DCF model...")
    try:
        fin_model = await run_financial_model(financials)
        base = fin_model.scenarios["base"]
        logs.append(
            f"[model] ✓ Base case: ${base.price_per_share:.2f}/share "
            f"({base.upside_downside_pct:+.1%} vs current ${base.current_price:.2f})"
        )
        logger.info("node_exit", node="model", status="ok")
        return {"financial_model": fin_model, "logs": logs}
    except Exception as exc:
        msg = f"[model] ✗ {exc}"
        logs.append(msg)
        logger.error("node_error", node="model", error=str(exc))
        return {"logs": logs, "errors": [msg], "status": "failed"}


async def node_report(state: dict) -> dict:
    logs = state.get("logs", [])
    financials: NormalisedFinancials = state["raw_financials"]
    fin_model = state["financial_model"]
    logger.info("node_enter", node="report", ticker=financials.ticker)
    logs.append("[report] Running 4-step report agent (plan → analyse → write → review)...")
    try:
        brief = await run_report_agent(financials, fin_model)
        logs.append("[report] ✓ Equity brief generated")
        logger.info("node_exit", node="report", status="ok")
        return {"equity_brief": brief, "logs": logs, "status": "complete"}
    except Exception as exc:
        msg = f"[report] ✗ {exc}"
        logs.append(msg)
        logger.error("node_error", node="report", error=str(exc))
        return {"logs": logs, "errors": [msg], "status": "failed"}


def should_continue(state: dict) -> str:
    """Route to END if pipeline has failed, otherwise continue."""
    if state.get("status") == "failed":
        return "end"
    return "continue"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    """Construct and compile the LangGraph pipeline."""
    workflow = StateGraph(dict)

    workflow.add_node("ingest", node_ingest)
    workflow.add_node("transform", node_transform)
    workflow.add_node("validate", node_validate)
    workflow.add_node("model", node_model)
    workflow.add_node("report", node_report)

    workflow.set_entry_point("ingest")

    # Each step checks for failure before proceeding
    workflow.add_conditional_edges("ingest", should_continue, {"continue": "transform", "end": END})
    workflow.add_conditional_edges("transform", should_continue, {"continue": "validate", "end": END})
    workflow.add_conditional_edges("validate", should_continue, {"continue": "model", "end": END})
    workflow.add_conditional_edges("model", should_continue, {"continue": "report", "end": END})
    workflow.add_edge("report", END)

    return workflow.compile()


# Singleton graph (compiled once at import time)
pipeline_graph = build_graph()


async def run_pipeline(ticker: str) -> PipelineState:
    """
    Execute the full pipeline for a given ticker.
    Returns a PipelineState with all outputs and logs.
    """
    initial_state = {
        "ticker": ticker.upper(),
        "raw_financials": None,
        "financial_model": None,
        "equity_brief": None,
        "logs": [],
        "errors": [],
        "status": "running",
    }

    logger.info("pipeline_start", ticker=ticker)
    final_state = await pipeline_graph.ainvoke(initial_state)
    logger.info("pipeline_end", ticker=ticker, status=final_state.get("status"))

    return PipelineState(
        ticker=final_state["ticker"],
        raw_financials=final_state.get("raw_financials"),
        financial_model=final_state.get("financial_model"),
        equity_brief=final_state.get("equity_brief"),
        logs=final_state.get("logs", []),
        errors=final_state.get("errors", []),
        status=final_state.get("status", "unknown"),
    )