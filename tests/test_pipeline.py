"""
tests/test_pipeline.py
----------------------
Unit and integration tests for the financial pipeline.
Run: pytest tests/ -v --tb=short
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.financial import (
    BalanceSheet,
    CashFlowStatement,
    CompanyProfile,
    FinancialMetrics,
    IncomeStatement,
    MarketData,
    NormalisedFinancials,
    Scenario,
    ScenarioAssumptions,
)
from pipelines.transform import compute_metrics, transform
from pipelines.validate import validate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_income():
    return IncomeStatement(
        period_end=date(2023, 9, 30),
        revenue=383_285_000_000,
        gross_profit=169_148_000_000,
        operating_income=114_301_000_000,
        ebitda=123_000_000_000,
        net_income=96_995_000_000,
        eps_diluted=6.13,
        shares_diluted=15_812_547_000,
    )


@pytest.fixture
def prior_income():
    return IncomeStatement(
        period_end=date(2022, 9, 24),
        revenue=394_328_000_000,
        gross_profit=170_782_000_000,
        operating_income=119_437_000_000,
        ebitda=130_000_000_000,
        net_income=99_803_000_000,
        eps_diluted=6.11,
        shares_diluted=16_325_819_000,
    )


@pytest.fixture
def sample_balance():
    return BalanceSheet(
        period_end=date(2023, 9, 30),
        cash_and_equivalents=29_965_000_000,
        total_assets=352_583_000_000,
        total_debt=111_088_000_000,
        total_equity=62_146_000_000,
        net_debt=81_123_000_000,
    )


@pytest.fixture
def sample_cf():
    return CashFlowStatement(
        period_end=date(2023, 9, 30),
        operating_cash_flow=110_543_000_000,
        capex=-10_959_000_000,
        free_cash_flow=99_584_000_000,
    )


@pytest.fixture
def sample_financials(sample_income, prior_income, sample_balance, sample_cf):
    return NormalisedFinancials(
        ticker="AAPL",
        profile=CompanyProfile(
            ticker="AAPL",
            name="Apple Inc.",
            sector="Technology",
            industry="Consumer Electronics",
            description="Apple designs and markets consumer electronics.",
            website="https://apple.com",
            headquarters="Cupertino, US",
            employees=161_000,
        ),
        market=MarketData(
            ticker="AAPL",
            price=189.30,
            market_cap=2_940_000_000_000,
            enterprise_value=3_015_000_000_000,
            pe_ratio=30.8,
            ev_ebitda=24.5,
            beta=1.24,
            as_of=date(2024, 1, 15),
        ),
        income_statements=[prior_income, sample_income],
        balance_sheets=[sample_balance],
        cash_flows=[sample_cf],
        metrics=[],
    )


# ---------------------------------------------------------------------------
# Transform tests
# ---------------------------------------------------------------------------

class TestComputeMetrics:
    def test_basic_metrics(self, sample_income, sample_balance, sample_cf):
        m = compute_metrics(sample_income, sample_balance, sample_cf)
        assert abs(m.gross_margin - 169_148 / 383_285) < 0.001
        assert abs(m.ebitda_margin - 123_000 / 383_285) < 0.001
        assert m.fcf_margin > 0.20  # Apple has excellent FCF conversion

    def test_revenue_growth(self, sample_income, prior_income, sample_balance, sample_cf):
        m = compute_metrics(sample_income, sample_balance, sample_cf, prior_income=prior_income)
        # Revenue declined slightly in FY23
        assert m.revenue_growth_yoy is not None
        assert m.revenue_growth_yoy < 0

    def test_no_prior_income(self, sample_income, sample_balance, sample_cf):
        m = compute_metrics(sample_income, sample_balance, sample_cf, prior_income=None)
        assert m.revenue_growth_yoy is None

    def test_safe_division_on_zero(self, sample_income, sample_balance, sample_cf):
        sample_income.revenue = 0
        m = compute_metrics(sample_income, sample_balance, sample_cf)
        assert m.gross_margin == 0.0  # safe_div returns 0


# ---------------------------------------------------------------------------
# Transform pipeline tests
# ---------------------------------------------------------------------------

class TestTransform:
    def test_transform_produces_metrics(self, sample_financials):
        # Need matching periods across all 3 statements
        # Add second period to balance and cf
        b2 = BalanceSheet(
            period_end=date(2022, 9, 24),
            cash_and_equivalents=23_000_000_000,
            total_assets=350_000_000_000,
            total_debt=120_000_000_000,
            total_equity=50_000_000_000,
            net_debt=97_000_000_000,
        )
        cf2 = CashFlowStatement(
            period_end=date(2022, 9, 24),
            operating_cash_flow=122_000_000_000,
            capex=-10_000_000_000,
            free_cash_flow=112_000_000_000,
        )
        sample_financials.balance_sheets.append(b2)
        sample_financials.cash_flows.append(cf2)
        result = transform(sample_financials)
        assert len(result.metrics) == 2


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------

class TestValidate:
    def test_passes_valid_data(self, sample_financials):
        # Add extra periods to pass coverage check
        for yr in range(2020, 2022):
            sample_financials.income_statements.append(
                IncomeStatement(
                    period_end=date(yr, 9, 30), revenue=3e11, gross_profit=1.2e11,
                    operating_income=9e10, ebitda=1e11, net_income=7e10,
                    eps_diluted=4.0, shares_diluted=1.6e10,
                )
            )
            sample_financials.balance_sheets.append(
                BalanceSheet(
                    period_end=date(yr, 9, 30), cash_and_equivalents=2e10,
                    total_assets=3e11, total_debt=9e10, total_equity=6e10, net_debt=7e10,
                )
            )
            sample_financials.cash_flows.append(
                CashFlowStatement(
                    period_end=date(yr, 9, 30), operating_cash_flow=9e10,
                    capex=-1e10, free_cash_flow=8e10,
                )
            )
        result = validate(sample_financials)
        assert result.passed

    def test_fails_on_insufficient_data(self, sample_financials):
        sample_financials.income_statements = sample_financials.income_statements[:1]
        result = validate(sample_financials)
        assert not result.passed
        assert len(result.errors) > 0

    def test_warns_on_revenue_drop(self, sample_financials):
        # Create a large revenue drop
        sample_financials.income_statements[1].revenue = (
            sample_financials.income_statements[0].revenue * 0.5
        )
        # Pad to pass coverage
        for yr in [2020, 2021]:
            sample_financials.income_statements.append(
                IncomeStatement(
                    period_end=date(yr, 9, 30), revenue=3e11, gross_profit=1.2e11,
                    operating_income=9e10, ebitda=1e11, net_income=7e10,
                    eps_diluted=4.0, shares_diluted=1.6e10,
                )
            )
            sample_financials.balance_sheets.append(
                BalanceSheet(
                    period_end=date(yr, 9, 30), cash_and_equivalents=2e10,
                    total_assets=3e11, total_debt=9e10, total_equity=6e10, net_debt=7e10,
                )
            )
            sample_financials.cash_flows.append(
                CashFlowStatement(
                    period_end=date(yr, 9, 30), operating_cash_flow=9e10,
                    capex=-1e10, free_cash_flow=8e10,
                )
            )
        result = validate(sample_financials)
        assert any("declined" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Model: DCF maths test
# ---------------------------------------------------------------------------

class TestDCF:
    def test_dcf_calculation(self):
        from agents.financial_model_agent import _run_dcf
        assumptions = ScenarioAssumptions(
            scenario=Scenario.BASE,
            revenue_growth_rates=[0.05, 0.05, 0.04, 0.04, 0.03],
            ebitda_margin=0.30,
            capex_pct_revenue=0.03,
            terminal_growth_rate=0.025,
            wacc=0.09,
        )
        result = _run_dcf(
            base_revenue=383_000_000_000,
            net_debt=80_000_000_000,
            shares_outstanding=15_800_000_000,
            current_price=189.0,
            assumptions=assumptions,
        )
        assert result.enterprise_value > 0
        assert result.equity_value > 0
        assert result.price_per_share > 0
        # Sanity: Apple base case should be in ballpark
        assert 50 < result.price_per_share < 500

    def test_terminal_value_dominates(self):
        """TV typically 60-80% of total EV in a DCF."""
        from agents.financial_model_agent import _run_dcf
        assumptions = ScenarioAssumptions(
            scenario=Scenario.BASE,
            revenue_growth_rates=[0.05] * 5,
            ebitda_margin=0.30,
            capex_pct_revenue=0.03,
            terminal_growth_rate=0.025,
            wacc=0.09,
        )
        result = _run_dcf(1e11, 0, 1e9, 100.0, assumptions)
        tv_pct = result.pv_terminal_value / result.enterprise_value
        assert 0.4 < tv_pct < 0.95  # terminal value is significant