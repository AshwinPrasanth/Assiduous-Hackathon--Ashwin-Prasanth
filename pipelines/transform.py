"""
pipelines/transform.py
----------------------
Transform → Convert raw ingested data into derived metrics.

Takes validated raw statements and produces FinancialMetrics for each period,
ready for the financial model agent.
"""

from __future__ import annotations

import structlog

from models.financial import (
    BalanceSheet,
    CashFlowStatement,
    FinancialMetrics,
    IncomeStatement,
    NormalisedFinancials,
)

logger = structlog.get_logger(__name__)


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that returns `default` instead of raising on zero denominator."""
    if denominator == 0:
        return default
    return numerator / denominator


def compute_metrics(
    income: IncomeStatement,
    balance: BalanceSheet,
    cash_flow: CashFlowStatement,
    prior_income: IncomeStatement | None = None,
) -> FinancialMetrics:
    """Derive ratios and growth rates for a single period."""
    revenue_growth = None
    if prior_income and prior_income.revenue:
        revenue_growth = _safe_div(
            income.revenue - prior_income.revenue, prior_income.revenue
        )

    return FinancialMetrics(
        period_end=income.period_end,
        revenue_growth_yoy=revenue_growth,
        gross_margin=_safe_div(income.gross_profit, income.revenue),
        ebitda_margin=_safe_div(income.ebitda, income.revenue),
        net_margin=_safe_div(income.net_income, income.revenue),
        fcf_margin=_safe_div(cash_flow.free_cash_flow, income.revenue),
        fcf_conversion=_safe_div(cash_flow.free_cash_flow, income.net_income),
        net_debt_to_ebitda=_safe_div(balance.net_debt, income.ebitda),
        return_on_equity=_safe_div(income.net_income, balance.total_equity),
    )


def transform(financials: NormalisedFinancials) -> NormalisedFinancials:
    """
    Compute metrics for all periods where all three statements are available.
    Aligns by period_end date.
    """
    # Build lookup dicts by period_end
    income_map = {s.period_end: s for s in financials.income_statements}
    balance_map = {s.period_end: s for s in financials.balance_sheets}
    cf_map = {s.period_end: s for s in financials.cash_flows}

    common_periods = sorted(
        set(income_map) & set(balance_map) & set(cf_map)
    )

    metrics = []
    for i, period in enumerate(common_periods):
        prior = income_map.get(common_periods[i - 1]) if i > 0 else None
        m = compute_metrics(
            income=income_map[period],
            balance=balance_map[period],
            cash_flow=cf_map[period],
            prior_income=prior,
        )
        metrics.append(m)
        logger.debug(
            "metrics_computed",
            period=period.isoformat(),
            revenue_growth=m.revenue_growth_yoy,
            ebitda_margin=f"{m.ebitda_margin:.1%}",
            fcf_margin=f"{m.fcf_margin:.1%}",
        )

    financials.metrics = metrics
    logger.info("transform_complete", ticker=financials.ticker, periods=len(metrics))
    return financials