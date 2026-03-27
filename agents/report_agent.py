"""
agents/report_agent.py
----------------------
Report Agent — multi-step agentic pipeline producing a structured equity brief.

Architecture (observable steps):
  Step 1 – Planner:    decides which sections to write and key themes
  Step 2 – Analyst:    extracts key facts and data points from financials
  Step 3 – Writer:     drafts each section with proper uncertainty labelling
  Step 4 – Reviewer:   checks consistency, flags speculative statements

Each step logs its output so the trace is fully observable.
"""

from __future__ import annotations

import json
from datetime import datetime

import anthropic
import structlog

from models.financial import (
    EquityBrief,
    FinancialModel,
    FundingOption,
    NormalisedFinancials,
    RiskFactor,
    Scenario,
)

logger = structlog.get_logger(__name__)
client = anthropic.Anthropic()

MODEL = "claude-sonnet-4-20250514"


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------

def _call_claude(system: str, user: str, max_tokens: int = 1200) -> str:
    """Single-turn Claude call with structured logging."""
    msg = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text.strip()


def _summarise_financials(financials: NormalisedFinancials, model: FinancialModel) -> str:
    """Build a compact text summary of key numbers for prompt injection."""
    metrics = financials.metrics[-3:] if financials.metrics else []
    incomes = sorted(financials.income_statements, key=lambda x: x.period_end)[-3:]
    base_dcf = model.scenarios[Scenario.BASE]
    upside_dcf = model.scenarios[Scenario.UPSIDE]
    downside_dcf = model.scenarios[Scenario.DOWNSIDE]

    lines = [
        f"Company: {financials.profile.name} ({financials.ticker})",
        f"Sector: {financials.profile.sector} | Industry: {financials.profile.industry}",
        f"Current Price: ${financials.market.price:.2f} | Market Cap: ${financials.market.market_cap/1e9:.1f}B",
        f"EV: ${financials.market.enterprise_value/1e9:.1f}B | EV/EBITDA: {financials.market.ev_ebitda or 'N/A'}",
        "",
        "Historical Financials:",
    ]
    for inc in incomes:
        m = next((x for x in metrics if x.period_end == inc.period_end), None)
        lines.append(
            f"  {inc.period_end.year}: Rev=${inc.revenue/1e9:.1f}B, "
            f"EBITDA margin={m.ebitda_margin:.1%}, FCF margin={m.fcf_margin:.1%}"
            if m else f"  {inc.period_end.year}: Rev=${inc.revenue/1e9:.1f}B"
        )
    lines += [
        "",
        "DCF Scenarios (5-year, FCF-based):",
        f"  Base:     Implied price ${base_dcf.price_per_share:.2f} ({base_dcf.upside_downside_pct:+.1%} vs current)",
        f"  Upside:   Implied price ${upside_dcf.price_per_share:.2f} ({upside_dcf.upside_downside_pct:+.1%} vs current)",
        f"  Downside: Implied price ${downside_dcf.price_per_share:.2f} ({downside_dcf.upside_downside_pct:+.1%} vs current)",
    ]
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

Return JSON with keys: "themes" (list of strings), "risks" (list of {{"title": str, "severity": str}}), "funding_options" (list of strings)
"""
    raw = _call_claude(system, user, max_tokens=600)
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError:
        plan = {"themes": ["Revenue growth", "Margin expansion", "FCF generation"], "risks": [], "funding_options": []}
    logger.debug("planner_output", plan=plan)
    return plan


# ---------------------------------------------------------------------------
# Step 2: Analyst
# ---------------------------------------------------------------------------

def _step_analyst(financials: NormalisedFinancials, summary: str, plan: dict) -> dict:
    logger.info("report_agent_step", step="2_analyst", ticker=financials.ticker)
    system = (
        "You are a financial analyst extracting key data points for a brief. "
        "Be precise with numbers. Flag anything uncertain with [ESTIMATE]. "
        "Return only a JSON object."
    )
    themes_text = "\n".join(f"- {t}" for t in plan.get("themes", []))
    user = f"""For {financials.profile.name} ({financials.ticker}), extract concise, data-backed insights for:
{themes_text}

Use this data:
{summary}

Return JSON with keys matching each theme as a short slug (e.g. "revenue_growth"), 
each value being 2-3 sentences with numbers. Mark estimates with [ESTIMATE].
"""
    raw = _call_claude(system, user, max_tokens=800)
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        insights = json.loads(raw.strip())
    except json.JSONDecodeError:
        insights = {}
    logger.debug("analyst_output", insights=insights)
    return insights


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
        "You are an experienced equity research writer producing a concise, professional brief. "
        "Always label speculative statements as [ESTIMATE] or [MODEL OUTPUT]. "
        "Do not present projections as facts. Return only a JSON object, no markdown fences."
    )
    user = f"""Write the following sections for {financials.profile.name} ({financials.ticker}).

Data summary:
{summary}

Analyst insights:
{json.dumps(insights, indent=2)}

Planned investment themes: {plan.get('themes', [])}
Planned risks: {plan.get('risks', [])}
Planned funding options: {plan.get('funding_options', [])}

Return a JSON object with these exact keys:
- "executive_summary": 3-4 sentences, the TL;DR for a busy investor
- "brand_and_positioning": 2-3 sentences on market position, competitive moat
- "financial_highlights": 3-4 sentences on key financial metrics with numbers
- "valuation_summary": 2-3 sentences on DCF results and what they imply
- "investment_recommendation": 1 paragraph, balanced view with uncertainty caveats

Be specific, cite numbers, and always flag estimates as [ESTIMATE].
"""
    raw = _call_claude(system, user, max_tokens=1400)
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        sections = json.loads(raw.strip())
    except json.JSONDecodeError:
        sections = {
            "executive_summary": "Data available but structured output failed.",
            "brand_and_positioning": summary[:300],
            "financial_highlights": summary[:300],
            "valuation_summary": "See DCF model.",
            "investment_recommendation": "Insufficient data for recommendation.",
        }
    return sections


# ---------------------------------------------------------------------------
# Step 4: Reviewer (quality gate)
# ---------------------------------------------------------------------------

def _step_reviewer(ticker: str, sections: dict) -> dict:
    logger.info("report_agent_step", step="4_reviewer", ticker=ticker)
    system = (
        "You are a compliance reviewer ensuring an AI-generated equity brief meets quality standards. "
        "Return only a JSON object."
    )
    user = f"""Review this equity brief for {ticker}. For each section, check:
1. Are speculative statements properly labelled [ESTIMATE] or [MODEL OUTPUT]?
2. Are there any factual inconsistencies?
3. Is the language appropriately hedged?

Sections:
{json.dumps(sections, indent=2)}

Return JSON with key "reviewed_sections" (same structure as input, with corrections applied)
and "review_notes" (list of strings noting any changes made).
"""
    raw = _call_claude(system, user, max_tokens=1200)
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        reviewed = result.get("reviewed_sections", sections)
        notes = result.get("review_notes", [])
    except json.JSONDecodeError:
        reviewed = sections
        notes = ["Reviewer parsing failed; original sections used."]

    for note in notes:
        logger.info("reviewer_note", ticker=ticker, note=note)
    return reviewed


# ---------------------------------------------------------------------------
# Risk factors and funding options builders
# ---------------------------------------------------------------------------

def _build_risk_factors(plan: dict, financials: NormalisedFinancials) -> list[RiskFactor]:
    risks = []
    for r in plan.get("risks", []):
        if isinstance(r, dict):
            risks.append(RiskFactor(
                title=r.get("title", "Risk"),
                description=r.get("description", ""),
                severity=r.get("severity", "Medium"),
            ))
        else:
            risks.append(RiskFactor(title=str(r), description="", severity="Medium"))

    # Add standard risks if fewer than 2
    if len(risks) < 2:
        risks.append(RiskFactor(
            title="Macro & Rate Sensitivity",
            description=(
                "Higher-for-longer interest rates increase WACC and compress terminal value in DCF models. "
                "This [ESTIMATE] could reduce our base case implied price by 10-20%."
            ),
            severity="Medium",
        ))
        risks.append(RiskFactor(
            title="Data Model Uncertainty",
            description=(
                "Financial projections are model outputs based on historical data and AI-derived assumptions. "
                "Actual results may differ materially. [MODEL OUTPUT]"
            ),
            severity="High",
        ))
    return risks


def _build_funding_options(plan: dict) -> list[FundingOption]:
    options = []
    raw_opts = plan.get("funding_options", [])
    # Generic template — the LLM planner populates these
    for opt in raw_opts[:3]:
        options.append(FundingOption(
            option=str(opt),
            rationale="Based on current capital structure and market conditions. [ESTIMATE]",
            pros=["Potential to accelerate growth", "Market conditions may be favourable"],
            cons=["Execution risk", "Market timing uncertainty"],
        ))
    if not options:
        options.append(FundingOption(
            option="Share Buyback Programme",
            rationale="If FCF generation remains robust and shares trade below intrinsic value [MODEL OUTPUT].",
            pros=["EPS accretive", "Signals management confidence"],
            cons=["Opportunity cost vs. reinvestment", "Price-dependent returns"],
        ))
    return options


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_report_agent(
    financials: NormalisedFinancials,
    model: FinancialModel,
) -> EquityBrief:
    """
    Run the 4-step report agent pipeline.
    Steps are sequential with full logging (observable trace).
    """
    logger.info("report_agent_start", ticker=financials.ticker)
    summary = _summarise_financials(financials, model)

    # Step 1: Plan
    plan = _step_planner(financials, summary)

    # Step 2: Analyse
    insights = _step_analyst(financials, summary, plan)

    # Step 3: Write
    sections = _step_writer(financials, model, summary, plan, insights)

    # Step 4: Review / quality gate
    reviewed = _step_reviewer(financials.ticker, sections)

    # Assemble final brief
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