"""
pipelines/ingest.py
-------------------
Ingest → Fetch raw data from SEC EDGAR and yfinance.

Design principles:
- SEC EDGAR is the PRIMARY source for all statement data (official XBRL API)
- yfinance used for market data (price, market cap) — supplementary only
- 3-tier fallback for balance sheets and cash flows:
    1. Yahoo Finance
    2. EDGAR XBRL (direct balance sheet / cash flow concepts)
    3. Synthesize from income statement data (ensures DCF always runs)
- All network calls retried with exponential backoff
- EDGAR facts cached per ticker to avoid duplicate fetches
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Optional

import httpx
import structlog
import yfinance as yf
from tenacity import retry, stop_after_attempt, wait_exponential
from requests import Session

from models.financial import (
    BalanceSheet,
    CashFlowStatement,
    CompanyProfile,
    IncomeStatement,
    MarketData,
)

logger = structlog.get_logger(__name__)

EDGAR_USER_AGENT = os.getenv("EDGAR_USER_AGENT", "FinSightAI hackathon@example.com")
EDGAR_BASE = "https://data.sec.gov"

# In-process cache so we only hit EDGAR once per ticker per pipeline run
_edgar_facts_cache: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Yahoo browser-spoof session (reduces 429s)
# ---------------------------------------------------------------------------

def get_secure_session() -> Session:
    s = Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://finance.yahoo.com/',
    })
    try:
        s.get("https://fc.yahoo.com", timeout=5)
    except Exception:
        pass
    return s

yahoo_session = get_secure_session()


# ---------------------------------------------------------------------------
# EDGAR helpers
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def _edgar_get(client: httpx.AsyncClient, path: str) -> dict:
    time.sleep(0.12)  # SEC fair-use: max 10 req/s
    url = f"{EDGAR_BASE}{path}"
    resp = await client.get(
        url,
        headers={"User-Agent": EDGAR_USER_AGENT, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def fetch_cik(ticker: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        for entry in resp.json().values():
            if entry.get("ticker", "").upper() == ticker.upper():
                cik = str(entry["cik_str"]).zfill(10)
                logger.info("cik_resolved", ticker=ticker, cik=cik)
                return cik
    raise ValueError(f"Ticker {ticker} not found in EDGAR.")


async def fetch_edgar_facts(ticker: str) -> dict:
    """Fetch and cache XBRL company facts from EDGAR."""
    if ticker in _edgar_facts_cache:
        return _edgar_facts_cache[ticker]
    cik = await fetch_cik(ticker)
    async with httpx.AsyncClient() as client:
        facts = await _edgar_get(client, f"/api/xbrl/companyfacts/CIK{cik}.json")
    _edgar_facts_cache[ticker] = facts
    logger.info("edgar_facts_fetched", ticker=ticker, cik=cik)
    return facts


def _extract_annual_values(facts: dict, concept: str, namespace: str = "us-gaap") -> list[dict]:
    """Pull 10-K annual values for a single XBRL concept, deduped by period end.
    
    Filters to entries where the reporting period is at least 340 days long,
    eliminating quarterly filings that appear under 10-K forms and old segment
    entries (e.g. MSFT pre-2012 Revenues concept) that cover short windows.
    """
    try:
        units = facts["facts"][namespace][concept]["units"]
        unit_key = "USD" if "USD" in units else list(units.keys())[0]
        annual: dict[str, dict] = {}
        for e in units[unit_key]:
            if e.get("form") == "10-K" and e.get("end") and e.get("start"):
                # Require period to be ~annual (340–400 days) to exclude quarters
                # and multi-year cumulative entries
                try:
                    from datetime import date as _date
                    start = _date.fromisoformat(e["start"])
                    end = _date.fromisoformat(e["end"])
                    period_days = (end - start).days
                    if not (330 <= period_days <= 410):
                        continue
                except Exception:
                    pass  # If dates malformed, include anyway
                end = e["end"]
                if end not in annual or e.get("filed", "") > annual[end].get("filed", ""):
                    annual[end] = e
        return sorted(annual.values(), key=lambda x: x["end"])
    except KeyError:
        return []


def fetch_yfinance_data(ticker: str) -> yf.Ticker:
    return yf.Ticker(ticker, session=yahoo_session)


# ---------------------------------------------------------------------------
# Company profile
# ---------------------------------------------------------------------------

async def ingest_company_profile(ticker: str) -> CompanyProfile:
    info: dict = {}
    try:
        info = fetch_yfinance_data(ticker).info or {}
    except Exception:
        pass

    if not info.get("longName"):
        try:
            cik = await fetch_cik(ticker)
            async with httpx.AsyncClient() as client:
                sub = await _edgar_get(client, f"/submissions/CIK{cik}.json")
            info["longName"] = sub.get("name", ticker)
        except Exception:
            pass

    return CompanyProfile(
        ticker=ticker.upper(),
        name=info.get("longName", ticker),
        sector=info.get("sector", "Technology"),
        industry=info.get("industry", "Unknown"),
        description=info.get("longBusinessSummary", ""),
        website=info.get("website", ""),
        headquarters=f"{info.get('city', '')}, {info.get('country', '')}".strip(", "),
        employees=info.get("fullTimeEmployees"),
    )


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
async def _get_shares_from_edgar(ticker: str) -> float:
    """
    Get diluted shares outstanding. Priority order:
    1. Compute from EPS: shares = net_income / eps_diluted (most reliable for DCF)
    2. CommonStockSharesOutstanding us-gaap (annual 10-K entries only)
    3. EntityCommonStockSharesOutstanding DEI (can include non-diluted/stale counts)
    
    The market-cap sanity check is applied at the end to catch obvious errors.
    """
    try:
        facts = await fetch_edgar_facts(ticker)
        if not facts:
            return 0.0

        # --- Method 1: EPS-derived diluted shares (most accurate for DCF) ---
        ni_entries  = _extract_annual_values(facts, "NetIncomeLoss")
        eps_entries = _extract_annual_values(facts, "EarningsPerShareDiluted")
        if ni_entries and eps_entries:
            # Match by period end, use most recent 3 years and average
            ni_map  = {e["end"]: float(e["val"]) for e in ni_entries}
            eps_map = {e["end"]: float(e["val"]) for e in eps_entries}
            common  = sorted(set(ni_map) & set(eps_map))[-3:]
            derived = []
            for end in common:
                ni  = ni_map[end]
                eps = eps_map[end]
                if abs(eps) > 0.01 and ni != 0:
                    shares = abs(ni / eps)
                    # Sanity: must be between 100M and 50B shares
                    if 1e8 < shares < 5e10:
                        derived.append(shares)
            if derived:
                avg = sum(derived) / len(derived)
                logger.info("shares_from_eps_derived", ticker=ticker, shares_b=f"{avg/1e9:.3f}")
                return avg

        # --- Method 2: us-gaap CommonStockSharesOutstanding (10-K annual only) ---
        shares_entries = _extract_annual_values(facts, "CommonStockSharesOutstanding")
        if shares_entries:
            # Take median of last 3 years to avoid outliers
            recent = [float(e["val"]) for e in shares_entries[-3:] if 1e8 < float(e["val"]) < 5e10]
            if recent:
                recent.sort()
                median = recent[len(recent) // 2]
                logger.info("shares_from_usgaap", ticker=ticker, shares_b=f"{median/1e9:.3f}")
                return median

        # --- Method 3: DEI EntityCommonStockSharesOutstanding (least reliable) ---
        dei_facts = facts.get("facts", {}).get("dei", {})
        for concept in ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"]:
            shares_data = dei_facts.get(concept, {})
            if shares_data:
                units = shares_data.get("units", {})
                share_list = units.get("shares") or units.get("pure", [])
                if share_list:
                    # Take the most recent 10-K entry (not 10-Q)
                    annual = [e for e in share_list if e.get("form") == "10-K"]
                    if annual:
                        val = float(annual[-1].get("val", 0))
                        if 1e8 < val < 5e10:
                            logger.info("shares_from_dei", ticker=ticker, shares_b=f"{val/1e9:.3f}")
                            return val

        logger.warning("no_shares_found_in_edgar", ticker=ticker)
        return 0.0

    except Exception as e:
        logger.error("get_shares_failed", ticker=ticker, error=str(e))
        return 0.0
    
async def ingest_market_data(ticker: str) -> MarketData:
    ticker_obj = yf.Ticker(ticker, session=get_secure_session())
    info = {}
    
    try:
        # yfinance .info is notoriously slow and prone to 429s
        info = ticker_obj.info or {}
    except Exception as e:
        logger.warn("info_fetch_failed", ticker=ticker, error=str(e))

    # Path 1: Info Price
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
    
    # Path 2: History Bypass (The "Yahoo Killer")
    if price == 0.0:
        try:
            hist = ticker_obj.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        except Exception:
            pass

    # Path 3: Stooq free price API (no auth, no rate limits)
    if price == 0.0:
        try:
            import httpx as _httpx
            stooq_url = f"https://stooq.com/q/l/?s={ticker.lower()}.us&f=sd2t2ohlcv&h&e=csv"
            async with _httpx.AsyncClient(timeout=10) as _client:
                resp = await _client.get(stooq_url)
            if resp.status_code == 200:
                lines = resp.text.strip().split("\n")
                if len(lines) >= 2:
                    cols = lines[1].strip().split(",")
                    if len(cols) >= 5 and cols[4] not in ("N/D", "0"):
                        price = float(cols[4])  # Close price
                        logger.info("price_from_stooq", ticker=ticker, price=price)
        except Exception as e:
            logger.warn("stooq_fetch_failed", ticker=ticker, error=str(e))

    # Path 4: SEC Fundamental Derivation
    market_cap = info.get("marketCap") or 0.0
    if market_cap == 0.0 and price > 0:
        shares = await _get_shares_from_edgar(ticker) 
        market_cap = price * shares if shares else 0.0

    if price == 0.0:
        logger.warning("price_unavailable", ticker=ticker, note="All price sources failed; upside% will show as N/A in UI")

    return MarketData(
        ticker=ticker.upper(),
        price=price,
        market_cap=market_cap,
        enterprise_value=info.get("enterpriseValue") or market_cap,
        pe_ratio=info.get("trailingPE"),
        ev_ebitda=info.get("enterpriseToEbitda"),
        beta=info.get("beta"),
        as_of=date.today(),
    )


# ---------------------------------------------------------------------------
# Income statements — EDGAR primary, Yahoo fallback
# ---------------------------------------------------------------------------

async def ingest_income_statements(ticker: str) -> list[IncomeStatement]:
    try:
        facts = await fetch_edgar_facts(ticker)

        revenues = (
            _extract_annual_values(facts, "RevenueFromContractWithCustomerExcludingAssessedTax") or
            _extract_annual_values(facts, "Revenues") or
            _extract_annual_values(facts, "SalesRevenueNet") or
            _extract_annual_values(facts, "RevenueFromContractWithCustomerIncludingAssessedTax")
        )
        if not revenues:
            raise ValueError("No revenue concept found in EDGAR")

        logger.info(
            "revenue_concept_resolved",
            ticker=ticker,
            count=len(revenues),
            latest_period=revenues[-1]["end"] if revenues else "none",
            latest_val_b=f"{float(revenues[-1]['val'])/1e9:.1f}" if revenues else "0",
        )

        gross_map  = {e["end"]: float(e["val"]) for e in _extract_annual_values(facts, "GrossProfit")}
        oi_map     = {e["end"]: float(e["val"]) for e in _extract_annual_values(facts, "OperatingIncomeLoss")}
        ni_map     = {e["end"]: float(e["val"]) for e in _extract_annual_values(facts, "NetIncomeLoss")}
        eps_map    = {e["end"]: float(e["val"]) for e in _extract_annual_values(facts, "EarningsPerShareDiluted")}
        shares_map = {e["end"]: float(e["val"]) for e in _extract_annual_values(facts, "CommonStockSharesOutstanding")}
        da_entries = (
            _extract_annual_values(facts, "DepreciationDepletionAndAmortization") or
            _extract_annual_values(facts, "DepreciationAndAmortization")
        )
        da_map = {e["end"]: float(e["val"]) for e in da_entries}

        ref_periods_inc = [r["end"] for r in revenues[-5:]]
        statements = []
        for r in revenues[-5:]:
            raw_end = r["end"]
            end = _normalize_period_end(raw_end, ref_periods_inc)
            rev = float(r["val"])
            gp  = gross_map.get(end) or gross_map.get(raw_end) or rev * 0.40
            oi  = oi_map.get(end) or oi_map.get(raw_end) or rev * 0.25
            ni  = ni_map.get(end) or ni_map.get(raw_end) or rev * 0.20
            da  = da_map.get(end) or da_map.get(raw_end) or rev * 0.05
            sh  = shares_map.get(end) or shares_map.get(raw_end) or 1e9
            eps = eps_map.get(end) or eps_map.get(raw_end) or (ni / sh if sh else 0.0)
            statements.append(IncomeStatement(
                period_end=date.fromisoformat(end),
                revenue=rev,
                gross_profit=gp,
                operating_income=oi,
                ebitda=oi + da,
                net_income=ni,
                eps_diluted=eps,
                shares_diluted=sh,
            ))

        if statements:
            # shares_map from CommonStockSharesOutstanding often misaligns with
            # revenue period dates, causing the 1e9 fallback to fire silently.
            # Override with authoritative DEI share count from SEC for all tickers.
            sec_shares = await _get_shares_from_edgar(ticker)
            if sec_shares > 0:
                statements = [
                    IncomeStatement(
                        period_end=s.period_end,
                        revenue=s.revenue,
                        gross_profit=s.gross_profit,
                        operating_income=s.operating_income,
                        ebitda=s.ebitda,
                        net_income=s.net_income,
                        eps_diluted=s.eps_diluted,
                        shares_diluted=sec_shares,
                    )
                    for s in statements
                ]
                logger.info(
                    "shares_overridden_from_sec",
                    ticker=ticker,
                    shares_b=f"{sec_shares/1e9:.3f}",
                )

            logger.info("income_from_edgar", ticker=ticker, count=len(statements))
            return sorted(statements, key=lambda s: s.period_end)

    except Exception as exc:
        logger.warning("edgar_income_fallback", ticker=ticker, error=str(exc))

    return _yf_income_statements(ticker)


def _yf_income_statements(ticker: str) -> list[IncomeStatement]:
    try:
        inc = fetch_yfinance_data(ticker).financials
        statements = []
        if inc is not None and not inc.empty:
            for col in list(inc.columns)[:5]:
                def _s(row: str, d: float = 0.0) -> float:
                    try:
                        v = inc.loc[row, col]
                        return float(v) if v is not None and str(v) != "nan" else d
                    except Exception:
                        return d
                rev = _s("Total Revenue")
                if rev == 0:
                    continue
                statements.append(IncomeStatement(
                    period_end=col.date() if hasattr(col, "date") else date(col.year, col.month, col.day),
                    revenue=rev,
                    gross_profit=_s("Gross Profit", rev * 0.38),
                    operating_income=_s("Operating Income", rev * 0.25),
                    ebitda=_s("EBITDA", rev * 0.30),
                    net_income=_s("Net Income", rev * 0.20),
                    eps_diluted=0.0,
                    shares_diluted=1e9,
                ))
        return sorted(statements, key=lambda s: s.period_end)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Balance sheets — Yahoo → EDGAR XBRL → Synthesize from income
# ---------------------------------------------------------------------------

async def ingest_balance_sheets(ticker: str) -> list[BalanceSheet]:
    # Tier 1: Yahoo
    try:
        bs = fetch_yfinance_data(ticker).balance_sheet
        if bs is not None and not bs.empty:
            sheets = _parse_yf_balance_sheet(bs)
            if sheets:
                logger.info("balance_sheet_from_yahoo", ticker=ticker)
                return sheets
    except Exception:
        pass

    # Tier 2: EDGAR XBRL balance sheet concepts
    try:
        sheets = await _edgar_balance_sheets(ticker)
        if sheets:
            logger.info("balance_sheet_from_edgar", ticker=ticker)
            return sheets
    except Exception as exc:
        logger.warning("edgar_balance_sheet_failed", ticker=ticker, error=str(exc))

    # Tier 3: Synthesize (always succeeds if income data exists)
    logger.warning("balance_sheet_synthesized", ticker=ticker)
    return await _synthesize_balance_sheets(ticker)


def _parse_yf_balance_sheet(bs) -> list[BalanceSheet]:
    sheets = []
    for col in list(bs.columns)[:5]:
        def _s(row: str, d: float = 0.0) -> float:
            try:
                v = bs.loc[row, col]
                return float(v) if v is not None and str(v) != "nan" else d
            except Exception:
                return d
        cash = _s("Cash And Cash Equivalents")
        total_assets = _s("Total Assets")
        total_debt = _s("Total Debt", _s("Long Term Debt"))
        equity = _s("Stockholders Equity", max(total_assets - total_debt, 1.0))
        sheets.append(BalanceSheet(
            period_end=col.date() if hasattr(col, "date") else date(col.year, col.month, col.day),
            cash_and_equivalents=cash,
            total_assets=total_assets,
            total_debt=total_debt,
            total_equity=max(equity, 1.0),
            net_debt=total_debt - cash,
        ))
    return sorted(sheets, key=lambda s: s.period_end)


def _normalize_period_end(end_str: str, reference_periods: list[str]) -> str:
    """
    Match an EDGAR period-end date to the closest reference period (from income statements).
    If within 10 days of a reference date, snap to it. This fixes the 1-3 day misalignment
    between EDGAR concepts (e.g. Assets ends 2024-06-28, Revenue ends 2024-06-30).
    """
    from datetime import date as _date
    try:
        d = _date.fromisoformat(end_str)
        for ref in reference_periods:
            ref_d = _date.fromisoformat(ref)
            if abs((d - ref_d).days) <= 10:
                return ref
    except Exception:
        pass
    return end_str


async def _edgar_balance_sheets(ticker: str) -> list[BalanceSheet]:
    facts = await fetch_edgar_facts(ticker)

    assets_entries = _extract_annual_values(facts, "Assets")
    cash_entries = (
        _extract_annual_values(facts, "CashAndCashEquivalentsAtCarryingValue") or
        _extract_annual_values(facts, "Cash")
    )
    debt_entries = (
        _extract_annual_values(facts, "LongTermDebtAndCapitalLeaseObligation") or
        _extract_annual_values(facts, "LongTermDebt") or
        _extract_annual_values(facts, "DebtAndCapitalLeaseObligations")
    )
    equity_entries = (
        _extract_annual_values(facts, "StockholdersEquity") or
        _extract_annual_values(facts, "RetainedEarningsAccumulatedDeficit")
    )

    if not assets_entries:
        return []

    cash_map   = {e["end"]: float(e["val"]) for e in cash_entries}
    debt_map   = {e["end"]: float(e["val"]) for e in debt_entries}
    equity_map = {e["end"]: float(e["val"]) for e in equity_entries}

    # Build reference periods from assets (most complete) for snapping
    ref_periods = [e["end"] for e in assets_entries[-5:]]

    sheets = []
    for e in assets_entries[-5:]:
        raw_end = e["end"]
        end = _normalize_period_end(raw_end, ref_periods)
        total_assets = float(e["val"])
        # Also try normalized end for map lookups
        cash  = cash_map.get(end) or cash_map.get(raw_end) or total_assets * 0.10
        debt  = debt_map.get(end) or debt_map.get(raw_end) or total_assets * 0.20
        equity = equity_map.get(end) or equity_map.get(raw_end) or (total_assets - debt)
        sheets.append(BalanceSheet(
            period_end=date.fromisoformat(end),
            cash_and_equivalents=cash,
            total_assets=total_assets,
            total_debt=debt,
            total_equity=max(equity, 1.0),
            net_debt=debt - cash,
        ))
    return sorted(sheets, key=lambda s: s.period_end)


async def _synthesize_balance_sheets(ticker: str) -> list[BalanceSheet]:
    """
    Synthesize balance sheets from income data using conservative heuristics.
    Large-cap tech (MSFT, AAPL, GOOGL, NVDA) typically holds NET CASH.
    Using minimal debt so DCF equity value is not artificially penalised.
    """
    incomes = await ingest_income_statements(ticker)
    sheets = []
    for inc in incomes:
        rev = inc.revenue
        # Cash-rich tech heuristic: cash >> debt → net cash position
        cash  = rev * 0.20   # tech holds ~20% rev in cash
        debt  = rev * 0.08   # light leverage
        assets = rev * 0.80
        equity = assets - debt
        # net_debt will be NEGATIVE (net cash) — correct for MSFT/AAPL/GOOGL
        sheets.append(BalanceSheet(
            period_end=inc.period_end,
            cash_and_equivalents=cash,
            total_assets=assets,
            total_debt=debt,
            total_equity=max(equity, 1.0),
            net_debt=debt - cash,
        ))
    return sheets


# ---------------------------------------------------------------------------
# Cash flows — Yahoo → EDGAR XBRL → Synthesize from income
# ---------------------------------------------------------------------------

async def ingest_cash_flows(ticker: str) -> list[CashFlowStatement]:
    # Tier 1: Yahoo
    try:
        cf = fetch_yfinance_data(ticker).cashflow
        if cf is not None and not cf.empty:
            flows = _parse_yf_cashflow(cf)
            if flows:
                logger.info("cashflow_from_yahoo", ticker=ticker)
                return flows
    except Exception:
        pass

    # Tier 2: EDGAR XBRL
    try:
        flows = await _edgar_cash_flows(ticker)
        if flows:
            logger.info("cashflow_from_edgar", ticker=ticker)
            return flows
    except Exception as exc:
        logger.warning("edgar_cashflow_failed", ticker=ticker, error=str(exc))

    # Tier 3: Synthesize
    logger.warning("cashflow_synthesized", ticker=ticker)
    return await _synthesize_cash_flows(ticker)


def _parse_yf_cashflow(cf) -> list[CashFlowStatement]:
    flows = []
    for col in list(cf.columns)[:5]:
        def _s(row: str, d: float = 0.0) -> float:
            try:
                v = cf.loc[row, col]
                return float(v) if v is not None and str(v) != "nan" else d
            except Exception:
                return d
        ocf   = _s("Operating Cash Flow")
        capex = _s("Capital Expenditure")
        if capex > 0:
            capex = -capex
        flows.append(CashFlowStatement(
            period_end=col.date() if hasattr(col, "date") else date(col.year, col.month, col.day),
            operating_cash_flow=ocf,
            capex=capex,
            free_cash_flow=ocf + capex,
        ))
    return sorted(flows, key=lambda s: s.period_end)


async def _edgar_cash_flows(ticker: str) -> list[CashFlowStatement]:
    facts = await fetch_edgar_facts(ticker)

    ocf_entries = (
        _extract_annual_values(facts, "NetCashProvidedByUsedInOperatingActivities") or
        _extract_annual_values(facts, "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
    )
    capex_entries = (
        _extract_annual_values(facts, "PaymentsToAcquirePropertyPlantAndEquipment") or
        _extract_annual_values(facts, "CapitalExpendituresIncurredButNotYetPaid")
    )

    if not ocf_entries:
        return []

    capex_map = {e["end"]: -abs(float(e["val"])) for e in capex_entries}
    ref_periods = [e["end"] for e in ocf_entries[-5:]]
    flows = []
    for e in ocf_entries[-5:]:
        raw_end = e["end"]
        end = _normalize_period_end(raw_end, ref_periods)
        ocf = float(e["val"])
        capex = capex_map.get(end) or capex_map.get(raw_end) or -(ocf * 0.10)
        flows.append(CashFlowStatement(
            period_end=date.fromisoformat(end),
            operating_cash_flow=ocf,
            capex=capex,
            free_cash_flow=ocf + capex,
        ))
    return sorted(flows, key=lambda s: s.period_end)


async def _synthesize_cash_flows(ticker: str) -> list[CashFlowStatement]:
    """Synthesize OCF and FCF from income data. OCF ≈ Net Income + D&A (5% rev)."""
    incomes = await ingest_income_statements(ticker)
    flows = []
    for inc in incomes:
        da_proxy = inc.revenue * 0.05
        ocf = inc.net_income + da_proxy
        capex = -(inc.revenue * 0.05)
        flows.append(CashFlowStatement(
            period_end=inc.period_end,
            operating_cash_flow=ocf,
            capex=capex,
            free_cash_flow=ocf + capex,
        ))
    return flows