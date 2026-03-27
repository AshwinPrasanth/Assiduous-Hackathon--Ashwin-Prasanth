"""
pipelines/validate.py
---------------------
Validate → Cross-check financial statements for consistency and flag anomalies.

Validation rules:
1. Revenue must be non-decreasing (flag large drops)
2. EBITDA margin must be within plausible industry range
3. Net debt must be consistent across balance sheets
4. FCF conversion (FCF/NI) must be plausible (not extreme outlier)
5. Minimum data coverage: at least 3 annual periods
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from models.financial import FinancialMetrics, NormalisedFinancials

logger = structlog.get_logger(__name__)


@dataclass
class ValidationResult:
    passed: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate(financials: NormalisedFinancials) -> ValidationResult:
    warnings: list[str] = []
    errors: list[str] = []

    # --- Coverage check ---
    n_income = len(financials.income_statements)
    n_balance = len(financials.balance_sheets)
    n_cf = len(financials.cash_flows)

    if n_income < 3 or n_balance < 3 or n_cf < 3:
        errors.append(
            f"Insufficient data: income={n_income}, balance={n_balance}, cf={n_cf}. "
            "Need at least 3 annual periods."
        )

    # --- Revenue trend ---
    revenues = [s.revenue for s in sorted(financials.income_statements, key=lambda x: x.period_end)]
    for i in range(1, len(revenues)):
        pct_change = (revenues[i] - revenues[i - 1]) / revenues[i - 1] if revenues[i - 1] else 0
        if pct_change < -0.20:
            warnings.append(
                f"Revenue declined {pct_change:.0%} in period {i} — investigate organic vs reported."
            )

    # --- EBITDA margin sanity ---
    for m in financials.metrics:
        if m.ebitda_margin < 0:
            warnings.append(f"Negative EBITDA margin in {m.period_end} ({m.ebitda_margin:.1%}).")
        if m.ebitda_margin > 0.80:
            warnings.append(f"Unusually high EBITDA margin {m.ebitda_margin:.1%} in {m.period_end} — verify.")

    # --- FCF conversion ---
    for m in financials.metrics:
        if abs(m.fcf_conversion) > 3.0:
            warnings.append(
                f"FCF conversion ratio {m.fcf_conversion:.1f}x in {m.period_end} is extreme — check capex data."
            )

    # --- Net debt consistency ---
    for bs in financials.balance_sheets:
        expected = bs.total_debt - bs.cash_and_equivalents
        if abs(bs.net_debt - expected) > 1e8:  # $100M tolerance
            warnings.append(
                f"Net debt inconsistency in {bs.period_end}: "
                f"recorded={bs.net_debt/1e9:.1f}B, calculated={expected/1e9:.1f}B"
            )

    passed = len(errors) == 0
    result = ValidationResult(passed=passed, warnings=warnings, errors=errors)

    for w in warnings:
        logger.warning("validation_warning", ticker=financials.ticker, msg=w)
    for e in errors:
        logger.error("validation_error", ticker=financials.ticker, msg=e)

    logger.info(
        "validation_complete",
        ticker=financials.ticker,
        passed=passed,
        warnings=len(warnings),
        errors=len(errors),
    )
    return result