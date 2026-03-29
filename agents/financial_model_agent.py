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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or "gsk_YOUR_ACTUAL_KEY_HERE"
LLM_CLIENT = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
LLM_MODEL = "llama-3.3-70b-versatile"


# ---------------------------------------------------------------------------
# Historical anchor computation
# ---------------------------------------------------------------------------

def _compute_historical_anchors(financials: NormalisedFinancials) -> dict:
    """
    Derive trailing-average metrics directly from ingested data.
    These are passed to the LLM as hard constraints, and used as
    fallback defaults if LLM output is implausible.
    """
    metrics = financials.metrics[-3:] if financials.metrics else []
    incomes = sorted(financials.income_statements, key=lambda x: x.period_end)
    cash_flows = sorted(financials.cash_flows, key=lambda x: x.period_end)

    # EBITDA margin — from computed metrics
    ebitda_margins = [m.ebitda_margin for m in metrics if 0.0 < m.ebitda_margin < 1.0]
    avg_ebitda = sum(ebitda_margins) / len(ebitda_margins) if ebitda_margins else 0.30

    # Revenue growth — from computed metrics
    growths = [m.revenue_growth_yoy for m in metrics if m.revenue_growth_yoy is not None]
    avg_growth = sum(growths) / len(growths) if growths else 0.08

    # Capex as % of revenue — computed directly from cash flows + income
    capex_pcts = []
    cf_map = {cf.period_end: cf for cf in cash_flows}
    for inc in incomes[-3:]:
        cf = cf_map.get(inc.period_end)
        if cf and inc.revenue > 0 and cf.capex != 0:
            capex_pcts.append(abs(cf.capex) / inc.revenue)
    avg_capex = sum(capex_pcts) / len(capex_pcts) if capex_pcts else 0.05

    # FCF margin
    fcf_margins = [m.fcf_margin for m in metrics if m.fcf_margin is not None]
    avg_fcf = sum(fcf_margins) / len(fcf_margins) if fcf_margins else 0.20

    return {
        "avg_ebitda": avg_ebitda,
        "avg_growth": avg_growth,
        "avg_capex": avg_capex,
        "avg_fcf": avg_fcf,
        "ebitda_margins": ebitda_margins,
        "growths": growths,
    }


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

def _build_assumptions_prompt(financials: NormalisedFinancials, anchors: dict) -> str:
    metrics = financials.metrics[-3:] if len(financials.metrics) >= 3 else financials.metrics
    hist = "\n".join(
        f"  {m.period_end}: rev_growth={m.revenue_growth_yoy and f'{m.revenue_growth_yoy:.1%}' or 'N/A'}, "
        f"ebitda_margin={m.ebitda_margin:.1%}, fcf_margin={m.fcf_margin:.1%}"
        for m in metrics
    )
    latest_income = sorted(financials.income_statements, key=lambda x: x.period_end)[-1]
    avg_ebitda = anchors["avg_ebitda"]
    avg_growth = anchors["avg_growth"]
    avg_capex  = anchors["avg_capex"]

    return f"""You are a senior equity research analyst building a 5-year DCF model.

Company: {financials.profile.name} ({financials.ticker})
Sector: {financials.profile.sector} | Industry: {financials.profile.industry}
Current price: ${financials.market.price:.2f}
Latest annual revenue: ${latest_income.revenue/1e9:.1f}B

Historical metrics (last 3 years):
{hist}

Computed trailing averages (USE THESE AS YOUR ANCHORS):
  EBITDA margin:   {avg_ebitda:.1%}
  Revenue growth:  {avg_growth:.1%}
  Capex / Revenue: {avg_capex:.1%}

RULES — violating these makes the model invalid:
1. Base EBITDA margin = trailing avg ({avg_ebitda:.1%}) ± 2%. Do NOT extrapolate beyond historical range.
2. Base revenue growth Year 1 ≈ trailing avg ({avg_growth:.1%}), moderating to GDP+premium by Year 5.
3. Base Capex/Revenue ≈ trailing avg ({avg_capex:.1%}) ± 1%. This is critical — do not use industry generic 10%.
4. Upside: +3-5% EBITDA margin, +2-4% higher growth, lower WACC by 0.5%.
5. Downside: -3-5% EBITDA margin, lower growth, higher WACC by 1%.
6. WACC range: 8.5-11.0% for large-cap tech. Reflect beta and leverage.
7. Terminal growth: 2.0-3.0% only. Never exceed 3.5%.
8. ALL values as decimals (0.08 = 8%). Never use whole numbers like 8 or 35.

Return ONLY valid JSON, no markdown:
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
}}"""


def _get_llm_assumptions(financials: NormalisedFinancials, anchors: dict) -> dict:
    import json
    prompt = _build_assumptions_prompt(financials, anchors)
    logger.info("llm_assumptions_request", ticker=financials.ticker)

    response = LLM_CLIENT.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    assumptions = json.loads(raw.strip())
    logger.info("llm_assumptions_received", ticker=financials.ticker, scenarios=list(assumptions.keys()))
    return assumptions


def _normalise_and_clamp(raw: dict, anchors: dict) -> dict:
    """
    Defensive normalisation:
    - Convert whole-number percentages (35.0) to decimals (0.35)
    - Clamp all values to plausible ranges
    - Fall back to historical anchors if LLM output is wildly off
    """
    def to_decimal(v, threshold=1.0):
        v = float(v)
        return v / 100.0 if v > threshold else v

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    ebitda = clamp(to_decimal(raw["ebitda_margin"]), 0.05, 0.75)
    capex  = clamp(to_decimal(raw["capex_pct_revenue"]), 0.01, 0.30)
    wacc   = clamp(to_decimal(raw.get("wacc", 0.09)), 0.07, 0.15)
    tgr    = clamp(to_decimal(raw.get("terminal_growth_rate", 0.025), threshold=0.5), 0.01, 0.035)
    growth = [clamp(to_decimal(g), -0.15, 0.50) for g in raw["revenue_growth_rates"]]

    # Sanity: if EBITDA margin deviates >15% from historical, snap back
    avg_ebitda = anchors["avg_ebitda"]
    if abs(ebitda - avg_ebitda) > 0.15:
        logger.warning("llm_ebitda_margin_clamped",
                       llm_value=ebitda, historical=avg_ebitda)
        ebitda = clamp(ebitda, avg_ebitda - 0.10, avg_ebitda + 0.10)

    # Sanity: capex — if >3x historical, snap to historical
    avg_capex = anchors["avg_capex"]
    if avg_capex > 0 and capex > avg_capex * 3:
        logger.warning("llm_capex_clamped", llm_value=capex, historical=avg_capex)
        capex = clamp(capex, avg_capex * 0.5, avg_capex * 2.0)

    return {
        "ebitda_margin": ebitda,
        "capex_pct_revenue": capex,
        "wacc": wacc,
        "terminal_growth_rate": tgr,
        "revenue_growth_rates": growth,
    }


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
    TAX_RATE = 0.21
    wacc = assumptions.wacc
    tgr  = assumptions.terminal_growth_rate

    projected: list[ProjectedYear] = []
    revenue = base_revenue

    for yr in range(1, 6):
        growth  = assumptions.revenue_growth_rates[yr - 1]
        revenue = revenue * (1 + growth)
        ebitda  = revenue * assumptions.ebitda_margin
        capex   = revenue * assumptions.capex_pct_revenue
        fcf     = ebitda * (1 - TAX_RATE) - capex
        df      = 1 / ((1 + wacc) ** yr)
        pv_fcf  = fcf * df
        projected.append(ProjectedYear(
            year=yr, revenue=revenue, ebitda=ebitda,
            fcf=fcf, discount_factor=df, pv_fcf=pv_fcf,
        ))

    terminal_fcf   = projected[-1].fcf * (1 + tgr)
    terminal_value = terminal_fcf / (wacc - tgr)
    pv_terminal    = terminal_value / ((1 + wacc) ** 5)
    sum_pv_fcf     = sum(y.pv_fcf for y in projected)
    enterprise_value = sum_pv_fcf + pv_terminal
    equity_value     = enterprise_value - net_debt
    price_per_share  = equity_value / shares_outstanding if shares_outstanding > 0 else 0
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
    latest_income  = sorted(financials.income_statements, key=lambda x: x.period_end)[-1]
    latest_balance = sorted(financials.balance_sheets,    key=lambda x: x.period_end)[-1]
    latest_cf      = sorted(financials.cash_flows,        key=lambda x: x.period_end)[-1]

    base_revenue  = latest_income.revenue
    net_debt      = latest_balance.net_debt
    shares        = latest_income.shares_diluted
    current_price = financials.market.price

    # Compute historical anchors BEFORE calling LLM
    anchors = _compute_historical_anchors(financials)

    logger.info(
        "financial_model_start",
        ticker=financials.ticker,
        base_revenue_b=f"{base_revenue/1e9:.1f}",
        shares_b=f"{shares/1e9:.3f}",
        net_debt_b=f"{net_debt/1e9:.1f}",
        current_price=current_price,
        anchor_ebitda=f"{anchors['avg_ebitda']:.1%}",
        anchor_capex=f"{anchors['avg_capex']:.1%}",
        anchor_growth=f"{anchors['avg_growth']:.1%}",
    )

    raw_assumptions = _get_llm_assumptions(financials, anchors)

    scenarios: dict[Scenario, DCFValuation] = {}
    scenario_map = {
        Scenario.BASE:     raw_assumptions["base"],
        Scenario.UPSIDE:   raw_assumptions["upside"],
        Scenario.DOWNSIDE: raw_assumptions["downside"],
    }

    for scenario, raw in scenario_map.items():
        cleaned = _normalise_and_clamp(raw, anchors)
        assumptions = ScenarioAssumptions(
            scenario=scenario,
            revenue_growth_rates=cleaned["revenue_growth_rates"],
            ebitda_margin=cleaned["ebitda_margin"],
            capex_pct_revenue=cleaned["capex_pct_revenue"],
            terminal_growth_rate=cleaned["terminal_growth_rate"],
            wacc=cleaned["wacc"],
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
            ebitda_margin=f"{assumptions.ebitda_margin:.1%}",
            capex_pct=f"{assumptions.capex_pct_revenue:.1%}",
            wacc=f"{assumptions.wacc:.1%}",
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


# ---------------------------------------------------------------------------
# Sensitivity table — WACC × Terminal Growth Rate grid
# ---------------------------------------------------------------------------

def build_sensitivity_table(
    model: FinancialModel,
    wacc_range: list[float] | None = None,
    tgr_range:  list[float] | None = None,
) -> dict:
    if wacc_range is None:
        wacc_range = [0.075, 0.080, 0.085, 0.090, 0.095, 0.100, 0.105]
    if tgr_range is None:
        tgr_range  = [0.015, 0.020, 0.025, 0.030, 0.035]

    base_scenario = (
        model.scenarios.get("base") or
        model.scenarios.get(Scenario.BASE) or
        next(
            (v for k, v in model.scenarios.items() if "base" in str(k).lower()),
            next(iter(model.scenarios.values()))
        )
    )
    base_assumptions = base_scenario.assumptions
    current_price    = base_scenario.current_price

    grid = []
    for wacc in wacc_range:
        row = []
        for tgr in tgr_range:
            modified = ScenarioAssumptions(
                scenario=Scenario.BASE,
                revenue_growth_rates=base_assumptions.revenue_growth_rates,
                ebitda_margin=base_assumptions.ebitda_margin,
                capex_pct_revenue=base_assumptions.capex_pct_revenue,
                terminal_growth_rate=tgr,
                wacc=wacc,
            )
            dcf = _run_dcf(
                base_revenue=model.base_year_revenue,
                net_debt=model.net_debt,
                shares_outstanding=model.shares_outstanding,
                current_price=current_price,
                assumptions=modified,
            )
            row.append(round(dcf.price_per_share, 2))
        grid.append(row)

    return {
        "wacc_values": wacc_range,
        "tgr_values":  tgr_range,
        "grid":        grid,
        "current_price": current_price,
        "base_wacc":   base_assumptions.wacc,
        "base_tgr":    base_assumptions.terminal_growth_rate,
    }