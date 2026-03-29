"""
agents/report_agent.py
----------------------
Report Agent — multi-step agentic pipeline producing a structured equity brief.
Corrected to use DCFValuation model and handle missing models gracefully.
"""

from __future__ import annotations

import json
from datetime import datetime
import os
from typing import Optional
from openai import OpenAI
import structlog

# Import the correct names based on your models/financial.py
from models.financial import (
    EquityBrief,
    FinancialModel,
    FundingOption,
    NormalisedFinancials,
    RiskFactor,
    Scenario,
    DCFValuation,
    ScenarioAssumptions
)
from agents.brand_agent import retrieve_brand_context

logger = structlog.get_logger(__name__)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") or "gsk_YOUR_ACTUAL_KEY_HERE"
client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)
MODEL = "llama-3.3-70b-versatile"


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------

def _call_llm(system: str, user: str, max_tokens: int = 1200) -> str:
    """Single-turn Groq call using the 'client' variable."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ],
        response_format={"type": "json_object"}, 
        temperature=0.2,
        max_tokens=max_tokens
    )
    return response.choices[0].message.content.strip()

def _summarise_financials(financials: NormalisedFinancials, model: FinancialModel) -> str:
    """Build a compact text summary of key numbers for prompt injection."""
    metrics = financials.metrics[-3:] if financials.metrics else []
    incomes = sorted(financials.income_statements, key=lambda x: x.period_end)[-3:]
    
    # Safely access scenarios using the Scenario Enum keys
    base_dcf = model.scenarios.get(Scenario.BASE)
    upside_dcf = model.scenarios.get(Scenario.UPSIDE)
    downside_dcf = model.scenarios.get(Scenario.DOWNSIDE)

    lines = [
        f"Company: {financials.profile.name} ({financials.ticker})",
        f"Sector: {financials.profile.sector} | Industry: {financials.profile.industry}",
        f"Current Price: ${financials.market.price:.2f} | Market Cap: ${financials.market.market_cap/1e9:.1f}B",
        f"EV: ${financials.market.enterprise_value/1e9:.1f}B | EV/EBITDA: {financials.market.ev_ebitda or 'N/A'}",
        "",
        "Historical Financials (SEC Data):",
    ]
    for inc in incomes:
        m = next((x for x in metrics if x.period_end == inc.period_end), None)
        if m:
            lines.append(
                f"  {inc.period_end.year}: Rev=${inc.revenue/1e9:.1f}B, "
                f"EBITDA margin={m.ebitda_margin:.1%}, FCF margin={m.fcf_margin:.1%}"
            )
        else:
            lines.append(f"  {inc.period_end.year}: Rev=${inc.revenue/1e9:.1f}B")
            
    lines += ["", "Valuation Analysis:"]
    # Check if this is a real model or a placeholder (price > 0)
    if base_dcf and base_dcf.price_per_share > 0:
        lines += [
            f"  Base Case:     Implied ${base_dcf.price_per_share:.2f} ({base_dcf.upside_downside_pct:+.1%})",
            f"  Upside Case:   Implied ${upside_dcf.price_per_share:.2f} ({upside_dcf.upside_downside_pct:+.1%})",
            f"  Downside Case: Implied ${downside_dcf.price_per_share:.2f} ({downside_dcf.upside_downside_pct:+.1%})",
        ]
    else:
        lines.append("  [NOTICE] Quantitative DCF metrics unavailable (Yahoo 429). Rely on SEC fundamental trends.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 1: Planner
# ---------------------------------------------------------------------------

def _step_planner(financials: NormalisedFinancials, summary: str) -> dict:
    logger.info("report_agent_step", step="1_planner", ticker=financials.ticker)
    system = (
        "You are a lead equity research strategist. Your job is to plan a focused equity brief. "
        "Return only a JSON object, no markdown."
    )
    user = f"""Given the following company summary, identify:
1. The 3 most important investment themes (positive or negative)
2. The 2 biggest risk factors to highlight
3. The most relevant funding/strategic options for this company right now

Company summary:
{summary}

Return JSON with keys: "themes" (list of strings), "risks" (list of {{"title": str, "description": str, "severity": str}}), "funding_options" (list of strings)
"""
    raw = _call_llm(system, user, max_tokens=600)
    try:
        return json.loads(raw)
    except Exception:
        return {"themes": ["Revenue growth", "Market positioning"], "risks": [], "funding_options": []}


# ---------------------------------------------------------------------------
# Step 2: Analyst
# ---------------------------------------------------------------------------

def _step_analyst(financials: NormalisedFinancials, summary: str, plan: dict) -> dict:
    logger.info("report_agent_step", step="2_analyst", ticker=financials.ticker)
    system = (
        "You are a financial analyst extracting key data points. Be precise with numbers. "
        "Flag anything uncertain with [ESTIMATE]. Return only a JSON object."
    )
    themes_text = "\n".join(f"- {t}" for t in plan.get("themes", []))
    user = f"""For {financials.profile.name}, extract concise, data-backed insights for:
{themes_text}

Use this data:
{summary}

Return JSON with keys matching each theme as a short slug, each value being 2-3 sentences.
"""
    raw = _call_llm(system, user, max_tokens=800)
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Step 3: Writer
# ---------------------------------------------------------------------------

def _step_writer(
    financials: NormalisedFinancials,
    model: FinancialModel,
    summary: str,
    plan: dict,
    insights: dict,
) -> dict:
    logger.info("report_agent_step", step="3_writer", ticker=financials.ticker)
    system = (
        "You are an experienced equity research writer. Always label speculative statements as [ESTIMATE]. "
        "Do not present projections as facts. Return only a JSON object."
    )

    # Pull brand/IR/news context from the vector store (RAG)
    brand_context = retrieve_brand_context(
        financials.ticker,
        query=f"{financials.profile.name} business model revenue growth competitive advantage strategy",
        k=4,
    )
    brand_section = f"\n\nBrand & Positioning Context (from IR page, 10-K MD&A, news):\n{brand_context}" if brand_context else ""

    user = f"""Write the following sections for {financials.profile.name}.

Data summary:
{summary}{brand_section}

Analyst insights:
{json.dumps(insights, indent=2)}

Return a JSON object with these exact keys:
- "executive_summary": 3-4 sentences TL;DR with specific numbers
- "brand_and_positioning": 2-3 sentences on competitive moat, market position, and key products/segments (use the brand context above if available)
- "financial_highlights": 3-4 sentences on revenue/margins with actual figures
- "valuation_summary": 2-3 sentences on valuation (cite DCF implied price and upside/downside if available)
- "investment_recommendation": 1 balanced paragraph with a clear stance and key catalysts/risks

Be specific, cite numbers, and flag estimates as [ESTIMATE].
"""
    raw = _call_llm(system, user, max_tokens=1400)
    try:
        return json.loads(raw)
    except Exception:
        return {"executive_summary": "Drafting failed, please check raw logs."}


# ---------------------------------------------------------------------------
# Step 4: Reviewer
# ---------------------------------------------------------------------------

def _step_reviewer(ticker: str, sections: dict) -> dict:
    logger.info("report_agent_step", step="4_reviewer", ticker=ticker)
    system = "You are a compliance reviewer. Ensure language is appropriately hedged. Return JSON only."
    user = f"""Review this brief for {ticker}. Check for consistency and hedging:
{json.dumps(sections, indent=2)}

Return JSON with key "reviewed_sections" (corrected structure).
"""
    raw = _call_llm(system, user, max_tokens=1200)
    try:
        result = json.loads(raw)
        return result.get("reviewed_sections", sections)
    except Exception:
        return sections


# ---------------------------------------------------------------------------
# Helpers & Public Entry
# ---------------------------------------------------------------------------

def _build_risk_factors(plan: dict, financials: NormalisedFinancials) -> list[RiskFactor]:
    risks = []
    for r in plan.get("risks", []):
        if isinstance(r, dict):
            risks.append(RiskFactor(
                title=r.get("title", "Market Risk"),
                description=r.get("description", "Standard market volatility."),
                severity=r.get("severity", "Medium"),
            ))
    if not risks:
        risks.append(RiskFactor(title="Macro Volatility", description="Interest rate sensitivity.", severity="Medium"))
    return risks


def _build_funding_options(plan: dict) -> list[FundingOption]:
    options = []
    for opt in plan.get("funding_options", [])[:2]:
        options.append(FundingOption(
            option=str(opt),
            rationale="Strategic alignment with current growth phase.",
            pros=["Capital injection"], cons=["Dilution risk"]
        ))
    return options


async def run_report_agent(
    financials: NormalisedFinancials,
    model: Optional[FinancialModel],
) -> EquityBrief:
    """
    Run the 4-step report agent pipeline.
    Injects a placeholder DCFValuation model if the quantitative node was skipped.
    """
    logger.info("report_agent_start", ticker=financials.ticker)

    # --- NULL-MODEL SAFETY GATE ---
    if model is None:
        logger.warning("report_qualitative_mode_active", ticker=financials.ticker)
        
        # Create empty assumptions required by DCFValuation model
        dummy_assumptions = ScenarioAssumptions(
            scenario=Scenario.BASE,
            revenue_growth_rates=[0.0, 0.0, 0.0, 0.0, 0.0],
            ebitda_margin=0.0,
            capex_pct_revenue=0.0
        )
        
        # Create a placeholder DCFValuation
        placeholder = DCFValuation(
            scenario=Scenario.BASE,
            assumptions=dummy_assumptions,
            projected_years=[],
            terminal_value=0.0,
            pv_terminal_value=0.0,
            enterprise_value=0.0,
            equity_value=0.0,
            price_per_share=0.0,
            current_price=financials.market.price,
            upside_downside_pct=0.0
        )
        
        # Create the FinancialModel object
        model = FinancialModel(
            ticker=financials.ticker,
            base_year_revenue=0.0,
            base_year_fcf=0.0,
            shares_outstanding=0.0,
            net_debt=0.0,
            scenarios={Scenario.BASE: placeholder, Scenario.UPSIDE: placeholder, Scenario.DOWNSIDE: placeholder}
        )

    summary = _summarise_financials(financials, model)
    plan = _step_planner(financials, summary)
    insights = _step_analyst(financials, summary, plan)
    sections = _step_writer(financials, model, summary, plan, insights)
    reviewed = _step_reviewer(financials.ticker, sections)

    brief = EquityBrief(
        ticker=financials.ticker,
        company_name=financials.profile.name,
        generated_at=datetime.utcnow().isoformat() + "Z",
        executive_summary=reviewed.get("executive_summary", ""),
        brand_and_positioning=reviewed.get("brand_and_positioning", ""),
        financial_highlights=reviewed.get("financial_highlights", ""),
        valuation_summary=reviewed.get("valuation_summary", ""),
        risk_factors=_build_risk_factors(plan, financials),
        funding_and_strategic_options=_build_funding_options(plan),
        investment_recommendation=reviewed.get("investment_recommendation", ""),
    )

    logger.info("report_agent_complete", ticker=financials.ticker)
    return brief