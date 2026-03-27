"""
pipelines/ingest.py
-------------------
Ingest → Fetch raw data from SEC EDGAR and yfinance.

Design principles:
- All network calls are idempotent (safe to retry)
- Data fetched once per ticker per day (cache-friendly)
- SEC EDGAR accessed via official API endpoints only (no aggressive scraping)
- yfinance for market data and supplementary fundamentals
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from typing import Optional

import httpx
import structlog
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential

from models.financial import (
    BalanceSheet,
    CashFlowStatement,
    CompanyProfile,
    IncomeStatement,
    MarketData,
)

logger = structlog.get_logger(__name__)

# SEC requires a descriptive User-Agent per their fair-use policy
EDGAR_USER_AGENT = os.getenv("EDGAR_USER_AGENT", "FinSightAI hackathon@example.com")
EDGAR_BASE = "https://data.sec.gov"


# ---------------------------------------------------------------------------
# SEC EDGAR helpers
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _edgar_get(client: httpx.AsyncClient, path: str) -> dict:
    """Rate-limited GET against the SEC EDGAR data API."""
    time.sleep(0.12)  # SEC fair-use: ≤10 req/s
    url = f"{EDGAR_BASE}{path}"
    logger.debug("edgar_request", url=url)
    resp = await client.get(
        url,
        headers={"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_cik(ticker: str) -> str:
    """Resolve ticker → CIK using EDGAR company-tickers endpoint."""
    async with httpx.AsyncClient() as client:
        data = await _edgar_get(client, "/submissions/")
        # Use company_tickers JSON (public, no auth)
        url = "https://www.sec.gov/files/company_tickers.json"
        resp = await client.get(url, headers={"User-Agent": EDGAR_USER_AGENT})
        resp.raise_for_status()
        tickers_map = resp.json()
        for entry in tickers_map.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                cik = str(entry["cik_str"]).zfill(10)
                logger.info("cik_resolved", ticker=ticker, cik=cik)
                return cik
    raise ValueError(f"Ticker {ticker} not found in EDGAR company tickers.")


async def fetch_edgar_facts(ticker: str) -> dict:
    """
    Fetch structured XBRL financial facts for a company from SEC EDGAR.
    Returns the full companyfacts JSON which includes all reported figures.
    """
    cik = await fetch_cik(ticker)
    async with httpx.AsyncClient() as client:
        facts = await _edgar_get(client, f"/api/xbrl/companyfacts/CIK{cik}.json")
    logger.info("edgar_facts_fetched", ticker=ticker, cik=cik)
    return facts


def _extract_annual_values(facts: dict, concept: str, namespace: str = "us-gaap") -> list[dict]:
    """
    Pull annual (10-K) values for a single XBRL concept.
    Returns list of {end: date, val: float, form: str}.
    """
    try:
        units = facts["facts"][namespace][concept]["units"]
        # Usually USD; sometimes shares
        unit_key = "USD" if "USD" in units else list(units.keys())[0]
        entries = units[unit_key]
        # Filter to annual 10-K filings only, deduplicate by period end
        annual = {}
        for e in entries:
            if e.get("form") == "10-K" and e.get("end"):
                end = e["end"]
                if end not in annual or e.get("filed", "") > annual[end].get("filed", ""):
                    annual[end] = e
        return sorted(annual.values(), key=lambda x: x["end"])
    except KeyError:
        logger.warning("concept_missing", concept=concept)
        return []


# ---------------------------------------------------------------------------
# yfinance helpers
# ---------------------------------------------------------------------------

def fetch_yfinance_data(ticker: str) -> yf.Ticker:
    """Thin wrapper so we can mock in tests."""
    return yf.Ticker(ticker)


# ---------------------------------------------------------------------------
# Public ingest functions
# ---------------------------------------------------------------------------

async def ingest_company_profile(ticker: str) -> CompanyProfile:
    yf_ticker = fetch_yfinance_data(ticker)
    info = yf_ticker.info
    return CompanyProfile(
        ticker=ticker.upper(),
        name=info.get("longName", ticker),
        sector=info.get("sector", "Unknown"),
        industry=info.get("industry", "Unknown"),
        description=info.get("longBusinessSummary", ""),
        website=info.get("website", ""),
        headquarters=f"{info.get('city', '')}, {info.get('country', '')}",
        employees=info.get("fullTimeEmployees"),
    )


async def ingest_market_data(ticker: str) -> MarketData:
    yf_ticker = fetch_yfinance_data(ticker)
    info = yf_ticker.info
    return MarketData(
        ticker=ticker.upper(),
        price=info.get("currentPrice") or info.get("regularMarketPrice", 0.0),
        market_cap=info.get("marketCap", 0.0),
        enterprise_value=info.get("enterpriseValue", 0.0),
        pe_ratio=info.get("trailingPE"),
        ev_ebitda=info.get("enterpriseToEbitda"),
        beta=info.get("beta"),
        as_of=date.today(),
    )


async def ingest_income_statements(ticker: str) -> list[IncomeStatement]:
    """
    Build annual income statements from SEC EDGAR XBRL facts.
    Falls back to yfinance if EDGAR data is incomplete.
    """
    try:
        facts = await fetch_edgar_facts(ticker)
        revenues = _extract_annual_values(facts, "Revenues")
        if not revenues:
            revenues = _extract_annual_values(facts, "RevenueFromContractWithCustomerExcludingAssessedTax")

        gross_profits = {e["end"]: e["val"] for e in _extract_annual_values(facts, "GrossProfit")}
        op_incomes = {e["end"]: e["val"] for e in _extract_annual_values(facts, "OperatingIncomeLoss")}
        net_incomes = {e["end"]: e["val"] for e in _extract_annual_values(facts, "NetIncomeLoss")}
        eps_diluted = {e["end"]: e["val"] for e in _extract_annual_values(facts, "EarningsPerShareDiluted")}
        shares = {e["end"]: e["val"] for e in _extract_annual_values(facts, "CommonStockSharesOutstanding")}

        statements = []
        for r in revenues[-5:]:  # Last 5 years
            end = r["end"]
            rev = r["val"]
            gp = gross_profits.get(end, rev * 0.38)  # fallback estimate
            oi = op_incomes.get(end, rev * 0.28)
            ni = net_incomes.get(end, rev * 0.22)
            # EBITDA: approximate as operating income + D&A (10% of revenue heuristic if unavailable)
            da = _get_da(facts, end) or rev * 0.05
            ebitda = oi + da
            eps = eps_diluted.get(end, ni / 1e9)
            sh = shares.get(end, 1e9)
            statements.append(
                IncomeStatement(
                    period_end=date.fromisoformat(end),
                    revenue=rev,
                    gross_profit=gp,
                    operating_income=oi,
                    ebitda=ebitda,
                    net_income=ni,
                    eps_diluted=eps,
                    shares_diluted=sh,
                )
            )
        if statements:
            logger.info("income_statements_from_edgar", ticker=ticker, count=len(statements))
            return statements
    except Exception as exc:
        logger.warning("edgar_fallback", ticker=ticker, error=str(exc))

    # yfinance fallback
    return _yf_income_statements(ticker)


def _get_da(facts: dict, end: str) -> Optional[float]:
    """Extract Depreciation & Amortisation for a given period end date."""
    da_entries = _extract_annual_values(facts, "DepreciationDepletionAndAmortization")
    for e in da_entries:
        if e["end"] == end:
            return e["val"]
    return None


def _yf_income_statements(ticker: str) -> list[IncomeStatement]:
    yf_ticker = fetch_yfinance_data(ticker)
    inc = yf_ticker.financials  # annual, columns = dates
    statements = []
    for col in list(inc.columns)[:5]:
        def _safe(row: str, default: float = 0.0) -> float:
            try:
                v = inc.loc[row, col]
                return float(v) if v is not None and str(v) != "nan" else default
            except Exception:
                return default

        rev = _safe("Total Revenue")
        if rev == 0:
            continue
        gp = _safe("Gross Profit", rev * 0.38)
        oi = _safe("Operating Income", rev * 0.25)
        ni = _safe("Net Income", rev * 0.2)
        ebitda = _safe("EBITDA", oi * 1.15)
        shares = float(yf_ticker.info.get("sharesOutstanding", 1e9))
        eps = ni / shares if shares else 0.0
        statements.append(
            IncomeStatement(
                period_end=col.date() if hasattr(col, "date") else date(col.year, col.month, col.day),
                revenue=rev,
                gross_profit=gp,
                operating_income=oi,
                ebitda=ebitda,
                net_income=ni,
                eps_diluted=eps,
                shares_diluted=shares,
            )
        )
    return sorted(statements, key=lambda s: s.period_end)


async def ingest_balance_sheets(ticker: str) -> list[BalanceSheet]:
    """Ingest balance sheet data via yfinance (simpler than EDGAR for balance sheet)."""
    yf_ticker = fetch_yfinance_data(ticker)
    bs = yf_ticker.balance_sheet
    sheets = []
    for col in list(bs.columns)[:5]:
        def _safe(row: str, default: float = 0.0) -> float:
            try:
                v = bs.loc[row, col]
                return float(v) if v is not None and str(v) != "nan" else default
            except Exception:
                return default

        cash = _safe("Cash And Cash Equivalents")
        total_assets = _safe("Total Assets")
        total_debt = _safe("Total Debt", _safe("Long Term Debt"))
        equity = _safe("Stockholders Equity", total_assets - total_debt)
        net_debt = total_debt - cash
        sheets.append(
            BalanceSheet(
                period_end=col.date() if hasattr(col, "date") else date(col.year, col.month, col.day),
                cash_and_equivalents=cash,
                total_assets=total_assets,
                total_debt=total_debt,
                total_equity=equity,
                net_debt=net_debt,
            )
        )
    return sorted(sheets, key=lambda s: s.period_end)


async def ingest_cash_flows(ticker: str) -> list[CashFlowStatement]:
    yf_ticker = fetch_yfinance_data(ticker)
    cf = yf_ticker.cashflow
    flows = []
    for col in list(cf.columns)[:5]:
        def _safe(row: str, default: float = 0.0) -> float:
            try:
                v = cf.loc[row, col]
                return float(v) if v is not None and str(v) != "nan" else default
            except Exception:
                return default

        ocf = _safe("Operating Cash Flow")
        capex = _safe("Capital Expenditure")  # already negative in yfinance
        if capex > 0:
            capex = -capex  # normalise to negative
        fcf = ocf + capex
        flows.append(
            CashFlowStatement(
                period_end=col.date() if hasattr(col, "date") else date(col.year, col.month, col.day),
                operating_cash_flow=ocf,
                capex=capex,
                free_cash_flow=fcf,
            )
        )
    return sorted(flows, key=lambda s: s.period_end)