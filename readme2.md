# FinSight AI — Corporate Finance Autopilot

> An agentic pipeline that ingests public financial data, scrapes investor materials, builds a live 3-scenario DCF model with sensitivity analysis, and generates a full institutional equity research brief — automatically, for any listed US company, in under two minutes.

**Built for the Assiduous Hackathon · March 2026**

[![Deployed Ewbsite](https://img.shields.io/badge/Website-Experience%20website%20here-red)](https://thefinproject.vercel.app/)
[![Demo Video](https://img.shields.io/badge/Demo-Watch%203min%20walkthrough-green)](./demo.md)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
---

## What It Does

Type a ticker. Hit Enter. In under two minutes:

- **Financial model** — 5-year DCF with Base, Upside, and Downside scenarios
- **Sensitivity table** — 35 implied prices across WACC × terminal growth rate grid
- **Equity research note** — brand positioning, financial highlights, valuation, recommendation, risk factors
- **Advisory brief** — BUY/SELL/HOLD rating, price targets, funding and strategic options

> IBM: **BUY · $266 base · $355 upside**
> Apple: **SELL · $105 base vs $255 current**
> Covers: **full sensitivity analysis, EV bridge, 4-tab dashboard**

---

## Quick Start

### Docker — one command

```bash
git clone https://github.com/ashwinprasanth/finsight-ai
cd finsight-ai
cp .env.example .env        # fill in GROQ_API_KEY and EDGAR_USER_AGENT
docker compose up --build
```

| Service | URL |
|---------|-----|
| Frontend | http://localhost:3000 |
| Backend API + Swagger | http://localhost:8000/docs |
| Live pipeline logs | `docker compose` stdout |

### Local Dev

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
npm run dev    # http://localhost:3000
```

**Tests**
```bash
cd backend
pytest tests/ -v --tb=short
```

---

## Environment Variables

```bash
GROQ_API_KEY=gsk_...                        # Required — free at console.groq.com
EDGAR_USER_AGENT=YourName your@email.com    # Required by SEC fair-use policy
LOG_LEVEL=INFO
NEXT_PUBLIC_API_URL=http://localhost:8000
```

---

## Architecture

```
╔══════════════════════════════════════════════════════════════════════════╗
║                            FINSIGHT AI                                   ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║   FRONTEND — Next.js 14 App Router       BACKEND — FastAPI               ║
║   ┌──────────────────────────┐          ┌──────────────────────────┐     ║
║   │  Ticker Input            │  POST    │  /api/pipeline/run       │     ║
║   │  Terminal Log (live)     │ ───────► │                          │     ║
║   │                          │          │  Single response:        │     ║
║   │  4 Tabs:                 │ ◄─────── │  ├─ logs[]               │     ║
║   │  ├─ Overview             │  JSON    │  ├─ report{}             │     ║
║   │  │  ├─ Rating badge      │          │  ├─ model{}              │     ║
║   │  │  ├─ Metric cards      │          │  └─ sensitivity{}        │     ║
║   │  │  ├─ Revenue chart     │          └──────────┬───────────────┘     ║
║   │  │  └─ Scenario table    │                     │                     ║
║   │  ├─ DCF Model            │                     ▼                     ║
║   │  │  ├─ 3 scenario cards  │          ┌──────────────────────────┐     ║
║   │  │  ├─ Sensitivity grid  │          │   LangGraph Orchestrator │     ║
║   │  │  └─ EV bridge         │          │                          │     ║
║   │  ├─ Research Note        │          │  State graph with        │     ║
║   │  │  ├─ Brand & Position  │          │  conditional edge routing│     ║
║   │  │  ├─ Financials        │          │  and observable logging  │     ║
║   │  │  ├─ Valuation         │          └──────────┬───────────────┘     ║
║   │  │  └─ Risk factors      │                     │                     ║
║   │  └─ Advisory             │          ┌──────────▼───────────────┐     ║
║   │     ├─ Rating + target   │          │     6 PIPELINE NODES     │     ║
║   │     ├─ Funding options   │          └──────────────────────────┘     ║
║   │     └─ Investment thesis │                                           ║
║   └──────────────────────────┘                                           ║
║                                                                          ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║                        PIPELINE NODES                                    ║
║                                                                          ║
║   ┌─────────┐   ┌───────────┐   ┌────────┐   ┌───────┐                   ║
║   │ 1.INGEST│──►│2.TRANSFORM│──►│3.VALID-│──►│4.BRAND│                   ║
║   │         │   │           │   │  ATE   │   │       │                   ║
║   │ EDGAR   │   │ margins   │   │ 6-pt   │   │ IR    │                   ║
║   │ XBRL    │   │ growth    │   │ check  │   │ page  │                   ║
║   │ Yahoo   │   │ FCF       │   │        │   │ MD&A  │                   ║
║   │ 3-tier  │   │ RoE       │   │ warn / │   │ News  │                   ║
║   │ fallback│   │ validated │   │ fail   │   │ → RAG │                   ║
║   └─────────┘   └───────────┘   └────────┘   └───┬───┘                   ║
║                                                  │                       ║
║                                    ┌─────────────┘                       ║
║                                    ▼                                     ║
║                         ┌─────────────────┐                              ║
║                         │   5. MODEL      │                              ║
║                         │                 │                              ║
║                         │ LLM proposes    │                              ║
║                         │ assumptions     │                              ║
║                         │ (anchored to    │                              ║
║                         │ historical data)│                              ║
║                         │                 │                              ║
║                         │ Python runs DCF │                              ║  
║                         │ + sensitivity   │                              ║
║                         │ (deterministic) │                              ║
║                         └────────┬────────┘                              ║
║                                  │                                       ║
║                                  ▼                                       ║
║                         ┌─────────────────┐                              ║
║                         │   6. REPORT     │                              ║
║                         │                 │                              ║ 
║                         │ Step 1: Planner │                              ║
║                         │ Step 2: Analyst │                              ║
║                         │ Step 3: Writer  │◄── RAG context injected      ║
║                         │ Step 4: Reviewer│                              ║
║                         └─────────────────┘                              ║
║                                                                          ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║   DATA SOURCES                         AI & VECTOR LAYER                 ║
║                                                                          ║
║   ┌─────────────────────────────┐      ┌────────────────────────────┐    ║
║   │ SEC EDGAR XBRL API          │      │ Groq — llama-3.3-70b       │    ║
║   │ data.sec.gov (official)     │      │                            │    ║
║   │ • Revenue, EBITDA, FCF      │      │ Role: propose scenario     │    ║
║   │ • Capex, net income, EPS    │      │ assumptions only           │    ║
║   │ • 10-K MD&A full text       │      │                            │    ║
║   │ • Shares via EPS method     │      │ Does NOT compute maths     │    ║
║   │                             │      │ Python does the DCF        │    ║
║   │ Yahoo Finance               │      │                            │    ║
║   │ • Current price             │      │ JSON structured outputs    │    ║
║   │ • Market cap, EV/EBITDA     │      │ Sub-second via Groq LPU    │    ║
║   │ • Beta, P/E ratio           │      └────────────────────────────┘    ║
║   │                             │                                        ║
║   │ Google News RSS             │      ┌────────────────────────────┐    ║
║   │ • Recent headlines          │      │ TF-IDF Vector Store        │    ║
║   │                             │      │ (brand_agent.py)           │    ║
║   │ Company IR Pages            │      │                            │    ║
║   │ • Investor relations text   │      │ Pure Python — no deps      │    ║
║   │ • Known overrides for       │      │ 400-word chunks            │    ║
║   │   AAPL, MSFT, GOOGL, NVDA   │      │ 80-word overlap            │    ║
║   └─────────────────────────────┘      │ Cosine similarity retrieval│    ║
║                                        └────────────────────────────┘    ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## Financial Model Detail

### DCF Formula (deterministic Python — not LLM)

```
FCF  =  EBITDA × (1 − 0.21)  −  Capex
TV   =  FCF_yr5 × (1 + TGR) / (WACC − TGR)     ← Gordon Growth Model
EV   =  Σ PV(FCF_yr1..5)  +  PV(TV)
Eq   =  EV  −  Net Debt
P    =  Eq  /  Diluted Shares
```

### Shares Outstanding — 3-tier priority

```
1. EPS-derived:  shares = NetIncomeLoss / EarningsPerShareDiluted
                 (averaged over 3 years — most accurate for DCF)
2. us-gaap XBRL: CommonStockSharesOutstanding (10-K annual, median of 3yr)
3. DEI fallback: EntityCommonStockSharesOutstanding (10-K filtered only)
+ Market cap sanity check: auto-corrected if >15% deviation from mktcap/price
```

### Sensitivity Table

```
             TGR  1.5%   2.0%   2.5%   3.0%   3.5%
WACC  7.5%  $XXX   $XXX   $XXX   $XXX   $XXX
      8.0%  $XXX   $XXX   $XXX   $XXX   $XXX
      8.5%  $XXX   $XXX   $XXX   $XXX   $XXX
      9.0%  $XXX   $XXX  [$XXX]  $XXX   $XXX   ← base case highlighted
      9.5%  $XXX   $XXX   $XXX   $XXX   $XXX
     10.0%  $XXX   $XXX   $XXX   $XXX   $XXX
     10.5%  $XXX   $XXX   $XXX   $XXX   $XXX

35 valuations. Pure Python. Zero additional LLM calls.
```

### LLM Assumptions — anchored to historical data

The LLM receives:
- Trailing avg EBITDA margin (computed from actuals)
- Trailing avg revenue growth (computed from actuals)
- **Trailing avg capex ratio (computed from cash flows) — with explicit instruction to use this, not hallucinate**

Post-LLM clamp layer enforces:
- Capex: historical actual ± 3%, never exceeds 8%
- WACC: 7% – 15%
- Terminal growth: 1% – 3.5%
- Revenue growth: −10% – +35%

---

## Project Structure

```
finsight-ai/
├── backend/
│   ├── agents/
│   │   ├── orchestrator.py          # LangGraph — 6 nodes, conditional routing
│   │   ├── brand_agent.py           # IR scraper + MD&A + News + TF-IDF RAG
│   │   ├── financial_model_agent.py # LLM assumptions + Python DCF + sensitivity
│   │   └── report_agent.py          # 4-step: plan → analyse → write → review
│   ├── pipelines/
│   │   ├── ingest.py                # EDGAR + Yahoo 3-tier data fetcher
│   │   ├── transform.py             # Margin and growth metric computation
│   │   └── validate.py              # 6-point consistency checker
│   ├── models/
│   │   └── financial.py             # All Pydantic v2 typed domain models
│   ├── api/
│   │   └── main.py                  # FastAPI + sensitivity endpoint
│   └── tests/
│       └── test_pipeline.py         # DCF maths, transform, validation
├── frontend/
│   └── src/app/
│       ├── page.tsx                 # Full dashboard — 4 tabs, 5 chart types
│       ├── globals.css              # Styles in CSS (SSR hydration safe)
│       └── layout.tsx
├── docker-compose.yml
├── .github/workflows/ci.yml         # CI — test + lint + docker on every push
├── README.md
└── WRITEUP.md
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/pipeline/run` | Full pipeline. Single response: logs + report + model + sensitivity |
| `GET` | `/api/report/{ticker}` | Cached equity brief |
| `GET` | `/api/model/{ticker}` | Cached 3-scenario financial model |
| `GET` | `/api/sensitivity/{ticker}` | WACC × TGR grid |
| `GET` | `/health` | Health check |

---

## Key Design Decisions

| Decision | Chosen | Rejected | Reason |
|----------|--------|----------|--------|
| LLM provider | Groq llama-3.3-70b | OpenAI GPT-4 | Sub-second inference, free tier, JSON mode |
| LLM role | Assumptions only | Full valuation | Keeps all maths deterministic and testable |
| Primary data | SEC EDGAR XBRL | Bloomberg / paid | Official, public domain, no ToS issues |
| Vector store | Custom TF-IDF | ChromaDB / Pinecone | Zero extra dependencies, pure Python |
| Orchestration | LangGraph | Function chain | Observable state, conditional routing |
| Balance sheets | 3-tier fallback | Yahoo only | Yahoo 429s at any scale |
| Shares | EPS-derived | DEI namespace | DEI returns stale/inflated counts |
| CSS | globals.css | Inline style tag | Prevents Next.js SSR hydration errors |
| API response | Single response | Polling / SSE | No cache invalidation on reload |

---

## Hackathon Criteria Coverage

| # | Criterion | How it's met |
|---|-----------|-------------|
| 1 | Brand & Positioning | `brand_agent.py` — IR page + 10-K MD&A + News → RAG → Research Note |
| 2 | Financial & Market Context | SEC EDGAR XBRL + Yahoo Finance + Google News RSS |
| 3 | Structured Output | 5-year DCF financial model + full equity research brief |
| 4 | Financial Reasoning Layer | Base/Upside/Downside DCF + 35-point sensitivity heatmap |
| 5 | Agentic / Multi-step AI | LangGraph 6-node pipeline + 4-step report agent, all logged |
| 6 | Ingest Data | 3-tier pipeline → typed Pydantic models → linked to all outputs |
| 7 | Visualise Data | Revenue chart, scenario bar, sensitivity heatmap, EV bridge, metric cards |
| 8 | Advisory | Rating badge + price targets + funding options + investment thesis |

---

## Limitations

- **yfinance rate limiting** — Yahoo 429s handled by 3-tier fallback. Synthesized balance sheets use sector heuristics and may be inaccurate for unusual capital structures
- **DCF simplification** — Uses EBITDA × (1-tax) - Capex as FCF proxy. Does not model D&A tax shield or working capital changes explicitly
- **LLM non-determinism** — Assumptions may differ slightly between runs. Clamp layer enforces plausible bounds
- **Data lag** — EDGAR filings appear 2–4 days after submission
- **US equities only** — EDGAR covers US-listed companies; international tickers will fail at CIK resolution

---

## Third-Party Data & APIs

| Source | Used for | Terms |
|--------|----------|-------|
| [SEC EDGAR XBRL](https://data.sec.gov) | Financial statements, 10-K filings | Public domain, free |
| [yfinance](https://github.com/ranaroussi/yfinance) | Market price, market cap | Yahoo Finance ToS; research use |
| [Groq API](https://console.groq.com) | LLM inference — llama-3.3-70b | Free tier available |
| Google News RSS | Recent headlines | Public RSS feed |
| Company IR pages | Brand & positioning text | Public websites |

---

*Built for the Assiduous Hackathon. All outputs are for educational purposes only and do not constitute investment advice. All projections are model outputs, not forecasts.*
