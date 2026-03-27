"""
Typed domain models for all financial data flowing through the pipeline.
Pydantic v2 — validation, serialisation, and JSON schema generation included.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Scenario(str, Enum):
    UPSIDE = "upside"
    BASE = "base"
    DOWNSIDE = "downside"


class FilingType(str, Enum):
    TEN_K = "10-K"
    TEN_Q = "10-Q"


# ---------------------------------------------------------------------------
# Raw Ingest Models
# ---------------------------------------------------------------------------

class IncomeStatement(BaseModel):
    period_end: date
    revenue: float = Field(..., description="Total revenue in USD")
    gross_profit: float
    operating_income: float
    ebitda: float
    net_income: float
    eps_diluted: float
    shares_diluted: float

    @field_validator("revenue", "gross_profit", "ebitda")
    @classmethod
    def must_be_positive(cls, v: float) -> float:
        if v < 0:
            raise ValueError("Expected non-negative value")
        return v


class BalanceSheet(BaseModel):
    period_end: date
    cash_and_equivalents: float
    total_assets: float
    total_debt: float
    total_equity: float
    net_debt: float  # total_debt - cash

    @model_validator(mode="after")
    def check_net_debt(self) -> "BalanceSheet":
        expected = self.total_debt - self.cash_and_equivalents
        if abs(self.net_debt - expected) > 1e6:
            raise ValueError(
                f"net_debt={self.net_debt} inconsistent with debt/cash ({expected})"
            )
        return self


class CashFlowStatement(BaseModel):
    period_end: date
    operating_cash_flow: float
    capex: float  # stored as negative by convention
    free_cash_flow: float  # operating_cash_flow + capex

    @model_validator(mode="after")
    def check_fcf(self) -> "CashFlowStatement":
        expected = self.operating_cash_flow + self.capex
        if abs(self.free_cash_flow - expected) > 1e6:
            raise ValueError(
                f"free_cash_flow={self.free_cash_flow} != ocf+capex ({expected})"
            )
        return self


class MarketData(BaseModel):
    ticker: str
    price: float
    market_cap: float
    enterprise_value: float
    pe_ratio: Optional[float] = None
    ev_ebitda: Optional[float] = None
    beta: Optional[float] = None
    as_of: date


class CompanyProfile(BaseModel):
    ticker: str
    name: str
    sector: str
    industry: str
    description: str
    website: str
    headquarters: str
    employees: Optional[int] = None


# ---------------------------------------------------------------------------
# Derived / Transformed Models
# ---------------------------------------------------------------------------

class FinancialMetrics(BaseModel):
    """Calculated ratios and growth rates derived from raw statements."""
    period_end: date
    revenue_growth_yoy: Optional[float] = None
    gross_margin: float
    ebitda_margin: float
    net_margin: float
    fcf_margin: float
    fcf_conversion: float  # FCF / Net Income
    net_debt_to_ebitda: float
    return_on_equity: float


class NormalisedFinancials(BaseModel):
    """Single source of truth after transform + validate steps."""
    ticker: str
    profile: CompanyProfile
    market: MarketData
    income_statements: list[IncomeStatement]
    balance_sheets: list[BalanceSheet]
    cash_flows: list[CashFlowStatement]
    metrics: list[FinancialMetrics]


# ---------------------------------------------------------------------------
# Financial Model (DCF + Scenarios)
# ---------------------------------------------------------------------------

class ScenarioAssumptions(BaseModel):
    scenario: Scenario
    revenue_growth_rates: list[float] = Field(
        ..., min_length=5, max_length=5,
        description="YoY revenue growth for years 1-5"
    )
    ebitda_margin: float = Field(..., ge=0, le=1)
    capex_pct_revenue: float = Field(..., ge=0, le=1)
    terminal_growth_rate: float = Field(default=0.025, ge=0, le=0.1)
    wacc: float = Field(default=0.09, ge=0.05, le=0.25)


class ProjectedYear(BaseModel):
    year: int
    revenue: float
    ebitda: float
    fcf: float
    discount_factor: float
    pv_fcf: float


class DCFValuation(BaseModel):
    scenario: Scenario
    assumptions: ScenarioAssumptions
    projected_years: list[ProjectedYear]
    terminal_value: float
    pv_terminal_value: float
    enterprise_value: float
    equity_value: float
    price_per_share: float
    current_price: float
    upside_downside_pct: float


class FinancialModel(BaseModel):
    ticker: str
    base_year_revenue: float
    base_year_fcf: float
    shares_outstanding: float
    net_debt: float
    scenarios: dict[Scenario, DCFValuation]


# ---------------------------------------------------------------------------
# Report Models
# ---------------------------------------------------------------------------

class RiskFactor(BaseModel):
    title: str
    description: str
    severity: str  # High / Medium / Low


class FundingOption(BaseModel):
    option: str
    rationale: str
    estimated_size: Optional[str] = None
    pros: list[str]
    cons: list[str]


class EquityBrief(BaseModel):
    ticker: str
    company_name: str
    generated_at: str
    executive_summary: str
    brand_and_positioning: str
    financial_highlights: str
    valuation_summary: str
    risk_factors: list[RiskFactor]
    funding_and_strategic_options: list[FundingOption]
    investment_recommendation: str
    disclaimer: str = (
        "This report was generated by an AI system using publicly available data. "
        "It is for educational purposes only and does not constitute investment advice. "
        "All projections are model outputs, not forecasts. Cite uncertainty where present."
    )


# ---------------------------------------------------------------------------
# Pipeline State (passed between agents in LangGraph)
# ---------------------------------------------------------------------------

class PipelineState(BaseModel):
    ticker: str
    raw_financials: Optional[NormalisedFinancials] = None
    financial_model: Optional[FinancialModel] = None
    equity_brief: Optional[EquityBrief] = None
    logs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    status: str = "pending"  # pending | running | complete | failed