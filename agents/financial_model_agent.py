"""
agents/financial_model_agent.py
--------------------------------
Financial Model Agent — builds a 5-year DCF with three scenarios.

Inputs:  NormalisedFinancials
Outputs: FinancialModel (Base, Upside, Downside DCF valuations)

Design:
- Derives scenario assumptions from historical metrics + LLM reasoning
- All maths is deterministic Python (not LLM) — the LLM only sets assumptions
- Fully transparent: every formula visible and testable
"""

from __future__ import annotations
from dotenv import load_dotenv
import math

from openai import OpenAI
import structlog
import os

from models.financial import (
    DCFValuation,
    FinancialModel,
    NormalisedFinancials,
    ProjectedYear,
    Scenario,
    ScenarioAssumptions,
)

logger = structlog.get_logger(__name__)
load_dotenv()
# --- YOUR REQUESTED GROQ CONFIG ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or "gsk_YOUR_ACTUAL_KEY_HERE"

LLM_CLIENT = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)
LLM_MODEL = "llama-3.3-70b-versatile"

# ---------------------------------------------------------------------------
# LLM: derive scenario assumptions from historical data
# ---------------------------------------------------------------------------

def _build_assumptions_prompt(financials: NormalisedFinancials) -> str:
    metrics = financials.metrics[-3:] if len(financials.metrics) >= 3 else financials.metrics
    hist = "\n".join(
        f"  {m.period_end}: rev_growth={m.revenue_growth_yoy and f'{m.revenue_growth_yoy:.1%}' or 'N/A'}, "
        f"ebitda_margin={m.ebitda_margin:.1%}, fcf_margin={m.fcf_margin:.1%}"
        for m in metrics
    )
    latest_income = sorted(financials.income_statements, key=lambda x: x.period_end)[-1]
    return f"""You are a senior equity research analyst. Based on the following historical financials for {financials.profile.name} ({financials.ticker}), 
define realistic Base, Upside, and Downside scenario assumptions for a 5-year DCF model.

Historical metrics (last 3 years):
{hist}

Latest annual revenue: ${latest_income.revenue/1e9:.1f}B
Sector: {financials.profile.sector}
Industry: {financials.profile.industry}

Return ONLY a JSON object with this exact structure (no markdown, no explanation):
{{
  "base": {{
    "revenue_growth_rates": [0.XX, 0.XX, 0.XX, 0.XX, 0.XX],
    "ebitda_margin": 0.XX,
    "capex_pct_revenue": 0.XX,
    "terminal_growth_rate": 0.025,
    "wacc": 0.09
  }},
  "upside": {{
    "revenue_growth_rates": [0.XX, 0.XX, 0.XX, 0.XX, 0.XX],
    "ebitda_margin": 0.XX,
    "capex_pct_revenue": 0.XX,
    "terminal_growth_rate": 0.03,
    "wacc": 0.085
  }},
  "downside": {{
    "revenue_growth_rates": [0.XX, 0.XX, 0.XX, 0.XX, 0.XX],
    "ebitda_margin": 0.XX,
    "capex_pct_revenue": 0.XX,
    "terminal_growth_rate": 0.02,
    "wacc": 0.10
  }}
}}

Rules:
- Revenue growth rates should reflect the company's realistic trajectory given historical performance
- Upside: optimistic but achievable (not fantasy)
- Downside: conservative but not catastrophic
- EBITDA margins should be consistent with historical range ± reasonable variance
- WACC: reflect sector risk (tech ~8-11%), not extreme
- All values as decimals (e.g. 0.08 = 8%)
"""


def _get_llm_assumptions(financials: NormalisedFinancials) -> dict:
    """Call Groq to derive scenario assumptions. Returns parsed JSON."""
    import json

    prompt = _build_assumptions_prompt(financials)
    logger.info("llm_assumptions_request", ticker=financials.ticker)

    # UPDATED: Using your LLM_CLIENT and LLM_MODEL
    response = LLM_CLIENT.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}, # Forces valid JSON
        temperature=0.1
    )
    
    # UPDATED: Accessing content via OpenAI-style response object
    raw = response.choices[0].message.content.strip()

    # The rest of your JSON parsing logic
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    assumptions = json.loads(raw.strip())
    logger.info("llm_assumptions_received", ticker=financials.ticker, scenarios=list(assumptions.keys()))
    return assumptions

# ---------------------------------------------------------------------------
# DCF maths — fully deterministic
# ---------------------------------------------------------------------------

def _run_dcf(
    base_revenue: float,
    net_debt: float,
    shares_outstanding: float,
    current_price: float,
    assumptions: ScenarioAssumptions,
) -> DCFValuation:
    """
    5-year explicit DCF + Gordon Growth Model terminal value.
    
    FCF = Revenue × EBITDA_margin × (1 - tax_rate) - Capex
    We use a simplified NOPAT approach: FCF ≈ EBITDA × (1-tax) - Capex
    Tax rate assumed 21% (US corporate).
    """
    TAX_RATE = 0.21
    wacc = assumptions.wacc
    tgr = assumptions.terminal_growth_rate

    projected: list[ProjectedYear] = []
    revenue = base_revenue

    for yr in range(1, 6):
        growth = assumptions.revenue_growth_rates[yr - 1]
        revenue = revenue * (1 + growth)
        ebitda = revenue * assumptions.ebitda_margin
        capex = revenue * assumptions.capex_pct_revenue
        # Simplified FCF: EBITDA * (1-tax) - capex (ignoring D&A tax shield simplification)
        fcf = ebitda * (1 - TAX_RATE) - capex
        discount_factor = 1 / ((1 + wacc) ** yr)
        pv_fcf = fcf * discount_factor
        projected.append(
            ProjectedYear(
                year=yr,
                revenue=revenue,
                ebitda=ebitda,
                fcf=fcf,
                discount_factor=discount_factor,
                pv_fcf=pv_fcf,
            )
        )

    # Terminal value (Gordon Growth Model on Year 5 FCF)
    terminal_fcf = projected[-1].fcf * (1 + tgr)
    terminal_value = terminal_fcf / (wacc - tgr)
    pv_terminal = terminal_value / ((1 + wacc) ** 5)

    sum_pv_fcf = sum(y.pv_fcf for y in projected)
    enterprise_value = sum_pv_fcf + pv_terminal
    equity_value = enterprise_value - net_debt
    price_per_share = equity_value / shares_outstanding if shares_outstanding > 0 else 0
    upside = (price_per_share - current_price) / current_price if current_price > 0 else 0

    return DCFValuation(
        scenario=assumptions.scenario,
        assumptions=assumptions,
        projected_years=projected,
        terminal_value=terminal_value,
        pv_terminal_value=pv_terminal,
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        price_per_share=price_per_share,
        current_price=current_price,
        upside_downside_pct=upside,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_financial_model(financials: NormalisedFinancials) -> FinancialModel:
    """Build the full 3-scenario financial model for a company."""
    latest_income = sorted(financials.income_statements, key=lambda x: x.period_end)[-1]
    latest_balance = sorted(financials.balance_sheets, key=lambda x: x.period_end)[-1]
    latest_cf = sorted(financials.cash_flows, key=lambda x: x.period_end)[-1]

    base_revenue = latest_income.revenue
    net_debt = latest_balance.net_debt
    shares = latest_income.shares_diluted
    current_price = financials.market.price

    logger.info(
        "financial_model_start",
        ticker=financials.ticker,
        base_revenue_b=f"{base_revenue/1e9:.1f}",
        current_price=current_price,
    )

    # LLM derives assumptions
    raw_assumptions = _get_llm_assumptions(financials)

    scenarios: dict[Scenario, DCFValuation] = {}

    scenario_map = {
        Scenario.BASE: raw_assumptions["base"],
        Scenario.UPSIDE: raw_assumptions["upside"],
        Scenario.DOWNSIDE: raw_assumptions["downside"],
    }

    for scenario, raw in scenario_map.items():
        assumptions = ScenarioAssumptions(
            scenario=scenario,
            revenue_growth_rates=raw["revenue_growth_rates"],
            ebitda_margin=raw["ebitda_margin"],
            capex_pct_revenue=raw["capex_pct_revenue"],
            terminal_growth_rate=raw.get("terminal_growth_rate", 0.025),
            wacc=raw.get("wacc", 0.09),
        )
        valuation = _run_dcf(
            base_revenue=base_revenue,
            net_debt=net_debt,
            shares_outstanding=shares,
            current_price=current_price,
            assumptions=assumptions,
        )
        scenarios[scenario] = valuation
        logger.info(
            "scenario_complete",
            scenario=scenario.value,
            implied_price=f"${valuation.price_per_share:.2f}",
            upside=f"{valuation.upside_downside_pct:.1%}",
        )

    model = FinancialModel(
        ticker=financials.ticker,
        base_year_revenue=base_revenue,
        base_year_fcf=latest_cf.free_cash_flow,
        shares_outstanding=shares,
        net_debt=net_debt,
        scenarios=scenarios,
    )
    logger.info("financial_model_complete", ticker=financials.ticker)
    return model
