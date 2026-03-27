# Assiduous-Hackathon--Ashwin-Prasanth

# FinSight AI — Corporate Finance Autopilot

> An agentic pipeline that ingests public financial data, builds a live 3-scenario financial model, and generates a full equity research brief — automatically.

[![Demo Video](https://img.shields.io/badge/Demo-Watch%203min%20walkthrough-red)](./demo.md)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

---

## Quick Start (Docker — recommended)

```bash
git clone https://github.com/yourname/finsight-ai
cd finsight-ai
cp .env.example .env          # add your ANTHROPIC_API_KEY
docker compose up --build
```

- **Frontend**: http://localhost:3000
- **Backend API**: http://localhost:8000/docs
- **Pipeline logs**: visible in `docker compose` stdout

---

## Quick Start (local dev)

**Backend**
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn api.main:app --reload --port 8000
```

**Frontend**
```bash
cd frontend
npm install
npm run dev        # http://localhost:3000
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      FinSight AI                            │
│                                                             │
│  Frontend (Next.js 14)          Backend (FastAPI)           │
│  ┌────────────────────┐         ┌────────────────────────┐  │
│  │  Dashboard UI      │◄───────►│  /api/pipeline/run     │  │
│  │  Financial Charts  │  REST + │  /api/model/{ticker}   │  │
│  │  Scenario Sliders  │  SSE    │  /api/report/{ticker}  │  │
│  └────────────────────┘         └──────────┬─────────────┘  │
│                                            │                │
│                                 ┌──────────▼─────────────┐  │
│                                 │   Orchestrator Agent    │  │
│                                 │  (LangGraph / Claude)   │  │
│                                 └──────────┬─────────────┘  │
│                                            │                │
│               ┌────────────────────────────┤                │
│               ▼                ▼           ▼                │
│        ┌──────────┐   ┌──────────────┐  ┌──────────┐       │
│        │ Ingest   │   │  Financial   │  │ Report   │       │
│        │ Agent    │   │  Model Agent │  │ Agent    │       │
│        └────┬─────┘   └──────┬───────┘  └────┬─────┘       │
│             │                │               │              │
│    ┌────────▼──────┐  ┌──────▼──────┐  ┌────▼──────┐       │
│    │ SEC EDGAR API │  │ DCF Model   │  │ Anthropic │       │
│    │ Yahoo Finance │  │ 3-Scenario  │  │ Claude    │       │
│    │ Company Site  │  │ Sensitivity │  │ API       │       │
│    └───────────────┘  └─────────────┘  └───────────┘       │
└─────────────────────────────────────────────────────────────┘
```

### Pipeline: Ingest → Transform → Validate → Output

1. **Ingest** — `pipelines/ingest.py`  
   - Fetches SEC EDGAR filings (10-K, 10-Q) via official API (no scraping)  
   - Pulls market data via `yfinance` (free, compliant)  
   - Extracts brand/positioning text from investor relations pages  

2. **Transform** — `pipelines/transform.py`  
   - Normalises raw financials into typed Pydantic models  
   - Calculates derived metrics (EBITDA, FCF, growth rates, margins)  

3. **Validate** — `pipelines/validate.py`  
   - Cross-checks reported vs. calculated figures  
   - Flags anomalies, missing periods, accounting changes  

4. **Model** — `agents/financial_model_agent.py`  
   - Builds a 5-year DCF with WACC and terminal value  
   - Three scenarios: Upside / Base / Downside with sensitised drivers  

5. **Report** — `agents/report_agent.py`  
   - Multi-step Claude agent: planner → analyst → writer  
   - Outputs a structured equity brief (positioning, financials, risks, recommendation)

---

## Key Design Decisions & Trade-offs

| Decision | Chosen | Alternative | Reason |
|----------|--------|-------------|--------|
| Data source | SEC EDGAR + yfinance | Bloomberg / paid API | Free, official, no ToS issues |
| Orchestration | LangGraph | LangChain LCEL | Better observable state graph |
| Financial model | Python + Pydantic | Excel/openpyxl | Testable, version-controllable |
| LLM | Claude claude-sonnet-4-20250514 | GPT-4 | Superior reasoning on structured financial data |
| Frontend | Next.js 14 App Router | Vite React | SSR for initial chart render, same stack as Assiduous |
| Charts | Recharts | D3 | Faster to ship, sufficient for this scope |

**Limitations**  
- Financial projections are models, not forecasts — label uncertainty explicitly  
- SEC EDGAR lag: filings appear 2–4 days after submission  
- yfinance is unofficial; for production, replace with a paid market data vendor  
- LLM outputs are non-deterministic; report quality varies across runs  

---

## Third-Party Data & APIs

| Source | Use | Terms |
|--------|-----|-------|
| [SEC EDGAR Full-Text Search](https://efts.sec.gov) | 10-K / 10-Q filings | Public domain, free |
| [yfinance](https://github.com/ranaroussi/yfinance) | Historical prices, fundamentals | Yahoo Finance ToS; research use |
| [Anthropic Claude API](https://anthropic.com) | LLM reasoning & report generation | Commercial API key required |

---

## Running Tests

```bash
cd backend
pytest tests/ -v --tb=short
```

---

## Environment Variables

```
ANTHROPIC_API_KEY=sk-ant-...     # Required
TICKER=AAPL                      # Default ticker (overridable via UI)
LOG_LEVEL=INFO
EDGAR_USER_AGENT=YourName your@email.com   # Required by SEC fair-use policy
```

---

*This project was built as part of the Assiduous Hackathon. All outputs are for educational purposes only and do not constitute investment advice.*
