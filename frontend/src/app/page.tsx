"use client";

import { useState, useRef, useEffect } from "react";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  Cell,
} from "recharts";

// ─── Types ───────────────────────────────────────────────────────────────────

interface ScenarioData {
  assumptions: {
    revenue_growth_rates: number[];
    ebitda_margin: number;
    capex_pct_revenue: number;
    wacc: number;
    terminal_growth_rate: number;
  };
  projected_years: {
    year: number;
    revenue: number;
    ebitda: number;
    fcf: number;
    pv_fcf: number;
  }[];
  price_per_share: number;
  current_price: number;
  upside_downside_pct: number;
  enterprise_value: number;
  equity_value: number;
  terminal_value: number;
  pv_terminal_value: number;
}

interface FinancialModel {
  ticker: string;
  base_year_revenue: number;
  base_year_fcf: number;
  shares_outstanding: number;
  net_debt: number;
  scenarios: Record<string, ScenarioData>;
}

interface Report {
  ticker: string;
  company_name: string;
  generated_at: string;
  executive_summary: string;
  brand_and_positioning: string;
  financial_highlights: string;
  valuation_summary: string;
  risk_factors: { title: string; description: string; severity: string }[];
  funding_and_strategic_options: { option: string; rationale: string; pros: string[]; cons: string[] }[];
  investment_recommendation: string;
  disclaimer: string;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const fmt = {
  bn: (v: number) => `$${(v / 1e9).toFixed(1)}B`,
  pct: (v: number) => `${(v * 100).toFixed(1)}%`,
  price: (v: number) => `$${v.toFixed(2)}`,
  signed: (v: number) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`,
  cagr: (rates: number[]) => {
    if (!rates?.length) return "N/A";
    const avg = rates.reduce((a, b) => a + b, 0) / rates.length;
    return `${(avg * 100).toFixed(1)}%`;
  },
};

function getScenario(model: FinancialModel, name: string): ScenarioData | undefined {
  const s = model?.scenarios;
  if (!s) return undefined;
  return s[name] || s[name.toUpperCase()] || s[`Scenario.${name.toUpperCase()}`];
}

function getRating(upside: number): { label: string; color: string; bg: string } {
  if (upside > 0.2) return { label: "STRONG BUY", color: "#00C853", bg: "rgba(0,200,83,0.1)" };
  if (upside > 0.05) return { label: "BUY", color: "#76FF03", bg: "rgba(118,255,3,0.08)" };
  if (upside > -0.05) return { label: "HOLD", color: "#FFD600", bg: "rgba(255,214,0,0.08)" };
  if (upside > -0.15) return { label: "REDUCE", color: "#FF6D00", bg: "rgba(255,109,0,0.08)" };
  return { label: "SELL", color: "#FF1744", bg: "rgba(255,23,68,0.08)" };
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function MetricCard({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: boolean }) {
  return (
    <div className={`metric-card ${accent ? "accent-card" : ""}`}>
      <p className="metric-label">{label}</p>
      <p className="metric-value">{value}</p>
      {sub && <p className="metric-sub">{sub}</p>}
    </div>
  );
}

function ScenarioBar({ scenario, data, current }: { scenario: string; data: ScenarioData; current: number }) {
  const colors: Record<string, string> = { downside: "#FF4444", base: "#C8A951", upside: "#00C853" };
  const color = colors[scenario] || "#C8A951";
  const upside = data.upside_downside_pct;

  return (
    <div className="scenario-row">
      <div className="scenario-name">
        <span className="scenario-dot" style={{ background: color }} />
        <span>{scenario.toUpperCase()}</span>
      </div>
      <div className="scenario-growth">{fmt.cagr(data.assumptions.revenue_growth_rates)}</div>
      <div className="scenario-margin">{fmt.pct(data.assumptions.ebitda_margin)}</div>
      <div className="scenario-wacc">{fmt.pct(data.assumptions.wacc)}</div>
      <div className="scenario-price" style={{ color }}>
        {fmt.price(data.price_per_share)}
        <span className="scenario-upside" style={{ color }}>
          {fmt.signed(upside)}
        </span>
      </div>
    </div>
  );
}

function WaterfallBar({ label, value, max }: { label: string; value: number; max: number }) {
  const pct = Math.min(100, (Math.abs(value) / max) * 100);
  return (
    <div className="waterfall-row">
      <span className="waterfall-label">{label}</span>
      <div className="waterfall-track">
        <div className="waterfall-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="waterfall-value">{fmt.bn(value)}</span>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function Home() {
  const [ticker, setTicker] = useState("");
  const [logs, setLogs] = useState<string[]>([]);
  const [status, setStatus] = useState<"idle" | "running" | "complete" | "failed">("idle");
  const [report, setReport] = useState<Report | null>(null);
  const [model, setModel] = useState<FinancialModel | null>(null);
  const [sensitivity, setSensitivity] = useState<any>(null);
  const [activeTab, setActiveTab] = useState<"overview" | "model" | "report" | "advisory">("overview");
  const logsRef = useRef<HTMLDivElement>(null);

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  useEffect(() => {
    if (logsRef.current) logsRef.current.scrollTop = logsRef.current.scrollHeight;
  }, [logs]);

  const addLog = (msg: string) => setLogs((p) => [...p, msg]);

  const startAnalysis = async () => {
    if (!ticker.trim() || status === "running") return;
    const t = ticker.trim().toUpperCase();

    setLogs([
      `[system] FinSight AI v1.0 — initiating pipeline for ${t}`,
      `[ingest] Connecting to SEC EDGAR XBRL API...`,
      `[ingest] Resolving CIK for ticker ${t}...`,
    ]);
    setStatus("running");
    setReport(null);
    setModel(null);
    setActiveTab("overview");

    try {
      // Proper log injection
      let logIdx = 0;
      const realLogs = [
        `[ingest] Fetching 10-K XBRL facts from EDGAR...`,
        `[ingest] Extracting revenue, EBITDA, FCF time series...`,
        `[transform] Computing derived metrics (margins, growth rates)...`,
        `[validate] Running 6-point consistency checks...`,
        `[model] LLM: deriving Base / Upside / Downside assumptions...`,
        `[model] Running 5-year DCF with Gordon Growth terminal value...`,
        `[report] Step 1/4 — Planner: identifying investment themes...`,
        `[report] Step 2/4 — Analyst: extracting numerical data points...`,
        `[report] Step 3/4 — Writer: drafting equity brief sections...`,
        `[report] Step 4/4 — Reviewer: compliance & hedging check...`,
      ];

      const logInterval = setInterval(() => {
        if (logIdx < realLogs.length) {
          addLog(realLogs[logIdx++]);
        } else {
          clearInterval(logInterval);
        }
      }, 2200);

      const response = await fetch(`${API_BASE}/api/pipeline/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: t }),
      });

      clearInterval(logInterval);

      if (!response.ok) throw new Error(`API Error: ${response.status}`);

      const pipelineData = await response.json();

      // Merge backend logs
      if (pipelineData.logs?.length) {
        setLogs(pipelineData.logs);
      }

      addLog(`[system] Pipeline complete — fetching results...`);

      const [reportRes, modelRes] = await Promise.all([
        fetch(`${API_BASE}/api/report/${t}`),
        fetch(`${API_BASE}/api/model/${t}`),
      ]);

      if (reportRes.ok) setReport(await reportRes.json());
      if (modelRes.ok) {
        const m = await modelRes.json();
        setModel(m);
      }
      // Sensitivity comes from the all-in-one pipeline response
      if (pipelineData.sensitivity) setSensitivity(pipelineData.sensitivity);
      if (pipelineData.model) setModel(pipelineData.model);

      addLog(`[system] ✓ Analysis complete. Pipeline: ingest→transform→validate→model→report`);
      setStatus("complete");
      setActiveTab("overview");
    } catch (err: any) {
      addLog(`[system] ✗ ${err.message}`);
      setStatus("failed");
    }
  };

  // Derived data for charts
  const baseScenario = model ? getScenario(model, "base") : undefined;
  const upsideScenario = model ? getScenario(model, "upside") : undefined;
  const downsideScenario = model ? getScenario(model, "downside") : undefined;
  const rating = baseScenario ? getRating(baseScenario.upside_downside_pct) : null;

  const revenueChartData =
    baseScenario?.projected_years.map((y, i) => ({
      year: `Y${y.year}`,
      base: y.revenue / 1e9,
      upside: (upsideScenario?.projected_years[i]?.revenue || y.revenue) / 1e9,
      downside: (downsideScenario?.projected_years[i]?.revenue || y.revenue) / 1e9,
    })) || [];

  const fcfChartData =
    baseScenario?.projected_years.map((y) => ({
      year: `Y${y.year}`,
      base: y.pv_fcf / 1e9,
      fcf: y.fcf / 1e9,
    })) || [];

  const scenarioCompare = [
    { name: "Downside", price: downsideScenario?.price_per_share || 0, color: "#FF4444" },
    { name: "Base", price: baseScenario?.price_per_share || 0, color: "#C8A951" },
    { name: "Current", price: baseScenario?.current_price || 0, color: "#8888A0" },
    { name: "Upside", price: upsideScenario?.price_per_share || 0, color: "#00C853" },
  ];

  return (
    <div className="shell">
      {/* ── Topbar ── */}
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">FinSight</span>
          <span className="brand-tag">Corporate Finance Autopilot</span>
        </div>
        <span className={`topbar-status ${status}`}>
          {status === "idle" && "● READY"}
          {status === "running" && "● PIPELINE RUNNING"}
          {status === "complete" && "● ANALYSIS COMPLETE"}
          {status === "failed" && "● PIPELINE FAILED"}
        </span>
      </header>

      {/* ── Search ── */}
      <div className="search-zone">
        <div className="search-row">
          <span className="search-prefix">TICKER /</span>
          <input
            className="search-input"
            placeholder="AAPL"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && startAnalysis()}
            maxLength={10}
          />
          <button className="search-btn" onClick={startAnalysis} disabled={status === "running"}>
            {status === "running" ? "RUNNING..." : "RUN ANALYSIS →"}
          </button>
        </div>
      </div>

      {/* ── Terminal ── */}
      <div className="terminal-wrap">
        <div className="terminal">
          <div className="terminal-header">
            <div className="terminal-dot" style={{ background: "#FF5F57" }} />
            <div className="terminal-dot" style={{ background: "#FFBD2E" }} />
            <div className="terminal-dot" style={{ background: "#28CA41" }} />
            <span className="terminal-title">
              pipeline.log — ingest → transform → validate → model → report
            </span>
          </div>
          <div className="terminal-body" ref={logsRef}>
            {logs.length === 0 && (
              <span style={{ color: "#2A2A40" }}>Awaiting ticker input...</span>
            )}
            {logs.map((line, i) => {
              const cls = line.includes("✓")
                ? "ok"
                : line.includes("✗")
                ? "err"
                : line.includes("⚠")
                ? "warn"
                : line.startsWith("[system]")
                ? "sys"
                : "";
              return (
                <div key={i} className={`log-line ${cls}`}>
                  {line}
                </div>
              );
            })}
            {status === "running" && <span className="log-cursor" />}
          </div>
        </div>
      </div>

      {/* ── Main content ── */}
      {status === "idle" && (
        <div className="empty-state fade-up">
          <p className="empty-title">FinSight</p>
          <p className="empty-sub">Enter a ticker to begin agentic analysis</p>
        </div>
      )}

      {(status === "complete" || (status === "running" && report)) && report && (
        <>
          {/* ── Tabs ── */}
          <div className="tabs-wrap fade-up fade-up-1">
            {(["overview", "model", "report", "advisory"] as const).map((t) => (
              <button
                key={t}
                className={`tab ${activeTab === t ? "active" : ""}`}
                onClick={() => setActiveTab(t)}
              >
                {t === "overview"
                  ? "Overview"
                  : t === "model"
                  ? "DCF Model"
                  : t === "report"
                  ? "Research Note"
                  : "Advisory"}
              </button>
            ))}
          </div>

          <div className="tab-body fade-up fade-up-2">
            {/* ────────────── OVERVIEW TAB ────────────── */}
            {activeTab === "overview" && (
              <div>
                <div className="overview-header">
                  <div>
                    <p className="overview-company">{report.company_name}</p>
                    <p className="overview-ticker">
                      {report.ticker} · Equity Research Memo ·{" "}
                      {new Date(report.generated_at).toLocaleDateString("en-US", {
                        month: "long",
                        day: "numeric",
                        year: "numeric",
                      })}
                    </p>
                  </div>
                  <div className="overview-meta">
                    <p className="overview-date">
                      GENERATED {new Date(report.generated_at).toLocaleTimeString()}
                    </p>
                    {rating && baseScenario && (
                      <div
                        className="rating-badge"
                        style={{
                          color: rating.color,
                          borderColor: rating.color,
                          background: rating.bg,
                        }}
                      >
                        <span>{rating.label}</span>
                        <span style={{ opacity: 0.7 }}>
                          12M: {fmt.price(baseScenario.price_per_share)}
                        </span>
                      </div>
                    )}
                  </div>
                </div>

                {/* Key metrics */}
                {baseScenario && (
                  <div className="metrics-grid">
                    <MetricCard
                      label="12M Target (Base)"
                      value={fmt.price(baseScenario.price_per_share)}
                      sub={`${fmt.signed(baseScenario.upside_downside_pct)} vs current`}
                      accent
                    />
                    <MetricCard
                      label="Current Price"
                      value={fmt.price(baseScenario.current_price)}
                    />
                    <MetricCard
                      label="Upside Target"
                      value={fmt.price(upsideScenario?.price_per_share || 0)}
                      sub={upsideScenario ? fmt.signed(upsideScenario.upside_downside_pct) : ""}
                    />
                    <MetricCard
                      label="Downside Target"
                      value={fmt.price(downsideScenario?.price_per_share || 0)}
                      sub={downsideScenario ? fmt.signed(downsideScenario.upside_downside_pct) : ""}
                    />
                    <MetricCard
                      label="Base EBITDA Margin"
                      value={fmt.pct(baseScenario.assumptions.ebitda_margin)}
                      sub={`WACC ${fmt.pct(baseScenario.assumptions.wacc)}`}
                    />
                  </div>
                )}

                {/* Charts */}
                {revenueChartData.length > 0 && (
                  <div className="charts-row">
                    <div className="chart-card">
                      <p className="chart-title">Projected Revenue by Scenario ($B)</p>
                      <ResponsiveContainer width="100%" height={160}>
                        <AreaChart data={revenueChartData}>
                          <XAxis
                            dataKey="year"
                            tick={{ fill: "#6A6A82", fontSize: 10, fontFamily: "JetBrains Mono" }}
                            axisLine={false}
                            tickLine={false}
                          />
                          <YAxis
                            tick={{ fill: "#6A6A82", fontSize: 10, fontFamily: "JetBrains Mono" }}
                            axisLine={false}
                            tickLine={false}
                          />
                          <Tooltip
                            content={({ active, payload, label }) =>
                              active && payload?.length ? (
                                <div className="custom-tooltip">
                                  <p style={{ color: "var(--accent)", marginBottom: 4 }}>{label}</p>
                                  {payload.map((p: any) => (
                                    <p key={p.name}>
                                      {p.name}: ${p.value?.toFixed(1)}B
                                    </p>
                                  ))}
                                </div>
                              ) : null
                            }
                          />
                          <Area
                            type="monotone"
                            dataKey="upside"
                            stroke="#00C853"
                            fill="rgba(0,200,83,0.05)"
                            strokeWidth={1.5}
                            dot={false}
                          />
                          <Area
                            type="monotone"
                            dataKey="base"
                            stroke="#C8A951"
                            fill="rgba(200,169,81,0.08)"
                            strokeWidth={2}
                            dot={false}
                          />
                          <Area
                            type="monotone"
                            dataKey="downside"
                            stroke="#FF4444"
                            fill="rgba(255,68,68,0.04)"
                            strokeWidth={1.5}
                            dot={false}
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                    <div className="chart-card">
                      <p className="chart-title">Implied Price — Scenario Range</p>
                      <ResponsiveContainer width="100%" height={160}>
                        <BarChart data={scenarioCompare} barSize={32}>
                          <XAxis
                            dataKey="name"
                            tick={{ fill: "#6A6A82", fontSize: 10, fontFamily: "JetBrains Mono" }}
                            axisLine={false}
                            tickLine={false}
                          />
                          <YAxis
                            tick={{ fill: "#6A6A82", fontSize: 10, fontFamily: "JetBrains Mono" }}
                            axisLine={false}
                            tickLine={false}
                            domain={["auto", "auto"]}
                          />
                          <Tooltip
                            content={({ active, payload }) =>
                              active && payload?.length ? (
                                <div className="custom-tooltip">
                                  <p>
                                    {payload[0].name}: $
                                    {(payload[0].value as number)?.toFixed(2)}
                                  </p>
                                </div>
                              ) : null
                            }
                          />
                          <Bar dataKey="price" radius={[3, 3, 0, 0]}>
                            {scenarioCompare.map((entry, index) => (
                              <Cell key={index} fill={entry.color} />
                            ))}
                          </Bar>
                          {baseScenario && (
                            <ReferenceLine
                              y={baseScenario.current_price}
                              stroke="rgba(255,255,255,0.2)"
                              strokeDasharray="4 4"
                            />
                          )}
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}

                {/* Scenario table */}
                {baseScenario && (
                  <>
                    <p className="section-label">
                      3-Scenario DCF Engine — 5 Year Explicit + Terminal Value
                    </p>
                    <div className="scenario-table">
                      <div className="scenario-header">
                        <span>Scenario</span>
                        <span>Rev. CAGR</span>
                        <span>EBITDA Mgn</span>
                        <span>WACC</span>
                        <span>Implied Price</span>
                      </div>
                      {[
                        { key: "upside", data: upsideScenario },
                        { key: "base", data: baseScenario },
                        { key: "downside", data: downsideScenario },
                      ].map(({ key, data }) =>
                        data ? (
                          <ScenarioBar
                            key={key}
                            scenario={key}
                            data={data}
                            current={baseScenario.current_price}
                          />
                        ) : null
                      )}
                    </div>
                  </>
                )}

                {/* Executive summary */}
                <div className="report-section" style={{ marginTop: 28 }}>
                  <p className="report-section-label">Executive Summary</p>
                  <p className="report-section-text">{report.executive_summary}</p>
                </div>
              </div>
            )}

            {/* ────────────── MODEL TAB ────────────── */}
            {activeTab === "model" && model && (
              <div>
                <p className="section-label" style={{ marginBottom: 20 }}>
                  Discounted Cash Flow — Base / Upside / Downside
                </p>
                <div className="model-grid">
                  {[
                    { key: "upside", data: upsideScenario, color: "#00C853" },
                    { key: "base", data: baseScenario, color: "#C8A951" },
                    { key: "downside", data: downsideScenario, color: "#FF4444" },
                  ].map(
                    ({ key, data, color }) =>
                      data && (
                        <div
                          className="model-card"
                          key={key}
                          style={{ borderColor: `${color}30` }}
                        >
                          <p className="model-card-name">{key.toUpperCase()} CASE</p>
                          <p className="model-card-price" style={{ color }}>
                            {fmt.price(data.price_per_share)}
                          </p>
                          <p className="model-card-upside" style={{ color }}>
                            {fmt.signed(data.upside_downside_pct)} vs $
                            {data.current_price.toFixed(2)}
                          </p>
                          <div className="model-divider" />
                          <div className="model-kv">
                            <span className="model-kv-label">Rev. CAGR</span>
                            <span className="model-kv-value">
                              {fmt.cagr(data.assumptions.revenue_growth_rates)}
                            </span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">EBITDA Margin</span>
                            <span className="model-kv-value">
                              {fmt.pct(data.assumptions.ebitda_margin)}
                            </span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">Capex / Rev</span>
                            <span className="model-kv-value">
                              {fmt.pct(data.assumptions.capex_pct_revenue)}
                            </span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">WACC</span>
                            <span className="model-kv-value">{fmt.pct(data.assumptions.wacc)}</span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">Terminal Growth</span>
                            <span className="model-kv-value">
                              {fmt.pct(data.assumptions.terminal_growth_rate)}
                            </span>
                          </div>
                          <div className="model-divider" />
                          <div className="model-kv">
                            <span className="model-kv-label">Enterprise Value</span>
                            <span className="model-kv-value">{fmt.bn(data.enterprise_value)}</span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">Equity Value</span>
                            <span className="model-kv-value">{fmt.bn(data.equity_value)}</span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">PV Terminal Val</span>
                            <span className="model-kv-value">{fmt.bn(data.pv_terminal_value)}</span>
                          </div>
                          <div className="model-kv">
                            <span className="model-kv-label">TV % of EV</span>
                            <span className="model-kv-value">
                              {data.enterprise_value
                                ? fmt.pct(data.pv_terminal_value / data.enterprise_value)
                                : "N/A"}
                            </span>
                          </div>
                        </div>
                      )
                  )}
                </div>

                {/* FCF waterfall chart */}
                {fcfChartData.length > 0 && (
                  <>
                    <p className="section-label">PV of Free Cash Flows — Base Case ($B)</p>
                    <div className="chart-card" style={{ marginBottom: 24 }}>
                      <ResponsiveContainer width="100%" height={140}>
                        <BarChart data={fcfChartData} barSize={28}>
                          <XAxis
                            dataKey="year"
                            tick={{ fill: "#6A6A82", fontSize: 10, fontFamily: "JetBrains Mono" }}
                            axisLine={false}
                            tickLine={false}
                          />
                          <YAxis
                            tick={{ fill: "#6A6A82", fontSize: 10, fontFamily: "JetBrains Mono" }}
                            axisLine={false}
                            tickLine={false}
                          />
                          <Tooltip
                            content={({ active, payload, label }) =>
                              active && payload?.length ? (
                                <div className="custom-tooltip">
                                  <p style={{ color: "var(--accent)" }}>{label}</p>
                                  {/* FIX 1: cast value to number before calling .toFixed() */}
                                  <p>
                                    FCF: $
                                    {(
                                      payload.find((p: any) => p.dataKey === "fcf")
                                        ?.value as number
                                    )?.toFixed(1)}
                                    B
                                  </p>
                                  <p>
                                    PV FCF: $
                                    {(
                                      payload.find((p: any) => p.dataKey === "base")
                                        ?.value as number
                                    )?.toFixed(1)}
                                    B
                                  </p>
                                </div>
                              ) : null
                            }
                          />
                          <Bar dataKey="fcf" fill="rgba(200,169,81,0.2)" radius={[3, 3, 0, 0]} />
                          <Bar dataKey="base" fill="#C8A951" radius={[3, 3, 0, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </>
                )}

                {/* Sensitivity table */}
                {sensitivity && (
                  <>
                    <p className="section-label" style={{ marginTop: 28 }}>
                      Sensitivity Analysis — Implied Price per Share (WACC × Terminal Growth Rate)
                    </p>
                    <div
                      style={{
                        background: "var(--surface)",
                        border: "1px solid var(--border)",
                        borderRadius: 6,
                        overflow: "hidden",
                        marginBottom: 24,
                      }}
                    >
                      {/* Header row */}
                      <div
                        style={{
                          display: "grid",
                          gridTemplateColumns: `110px repeat(${sensitivity.tgr_values.length}, 1fr)`,
                          borderBottom: "1px solid var(--border)",
                          background: "rgba(255,255,255,0.03)",
                        }}
                      >
                        <div
                          style={{
                            padding: "10px 14px",
                            fontFamily: "JetBrains Mono, monospace",
                            fontSize: 9,
                            color: "var(--muted)",
                            textTransform: "uppercase",
                            letterSpacing: "0.15em",
                          }}
                        >
                          WACC \ TGR
                        </div>
                        {sensitivity.tgr_values.map((tgr: number) => (
                          <div
                            key={tgr}
                            style={{
                              padding: "10px 8px",
                              fontFamily: "JetBrains Mono, monospace",
                              fontSize: 10,
                              color:
                                tgr === sensitivity.base_tgr ? "var(--accent)" : "var(--muted)",
                              textAlign: "center",
                              fontWeight: tgr === sensitivity.base_tgr ? 600 : 400,
                            }}
                          >
                            {(tgr * 100).toFixed(1)}%
                          </div>
                        ))}
                      </div>
                      {/* Data rows */}
                      {sensitivity.wacc_values.map((wacc: number, wi: number) => (
                        <div
                          key={wacc}
                          style={{
                            display: "grid",
                            gridTemplateColumns: `110px repeat(${sensitivity.tgr_values.length}, 1fr)`,
                            borderBottom:
                              wi < sensitivity.wacc_values.length - 1
                                ? "1px solid var(--border)"
                                : "none",
                          }}
                        >
                          <div
                            style={{
                              padding: "10px 14px",
                              fontFamily: "JetBrains Mono, monospace",
                              fontSize: 10,
                              color:
                                wacc === sensitivity.base_wacc ? "var(--accent)" : "var(--muted)",
                              fontWeight: wacc === sensitivity.base_wacc ? 600 : 400,
                            }}
                          >
                            {(wacc * 100).toFixed(1)}%
                          </div>
                          {sensitivity.grid[wi].map((price: number, ti: number) => {
                            const tgr = sensitivity.tgr_values[ti];
                            const isBase =
                              wacc === sensitivity.base_wacc && tgr === sensitivity.base_tgr;
                            const upside =
                              (price - sensitivity.current_price) / sensitivity.current_price;
                            const cellColor =
                              upside > 0.15
                                ? "#00C853"
                                : upside > 0
                                ? "#76FF03"
                                : upside > -0.15
                                ? "#FFD600"
                                : upside > -0.3
                                ? "#FF6D00"
                                : "#FF4444";
                            return (
                              <div
                                key={ti}
                                style={{
                                  padding: "10px 8px",
                                  fontFamily: "JetBrains Mono, monospace",
                                  fontSize: 12,
                                  textAlign: "center",
                                  color: isBase ? "var(--ink)" : cellColor,
                                  background: isBase ? "var(--accent)" : `${cellColor}12`,
                                  fontWeight: isBase ? 700 : 400,
                                  border: isBase ? "none" : undefined,
                                }}
                              >
                                ${price.toFixed(0)}
                              </div>
                            );
                          })}
                        </div>
                      ))}
                      <div
                        style={{
                          padding: "8px 14px",
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 9,
                          color: "var(--muted)",
                          borderTop: "1px solid var(--border)",
                        }}
                      >
                        Highlighted cell = base case assumptions. Color: green = upside &gt;15%,
                        yellow = flat, red = downside &gt;15%. Current price: $
                        {sensitivity.current_price.toFixed(2)}
                      </div>
                    </div>
                  </>
                )}

                {/* FIX 2: wrap EV Bridge in {baseScenario && (...)} so the closing )} is valid */}
                {baseScenario && (
                  <>
                    <p className="section-label">EV Bridge — Base Case</p>
                    <div className="chart-card">
                      {[
                        {
                          label: "PV FCF (Yr 1–5)",
                          value: baseScenario.projected_years.reduce(
                            (a, y) => a + y.pv_fcf,
                            0
                          ),
                        },
                        { label: "PV Terminal Value", value: baseScenario.pv_terminal_value },
                        { label: "Enterprise Value", value: baseScenario.enterprise_value },
                        { label: "Less: Net Debt", value: -model.net_debt },
                        { label: "Equity Value", value: baseScenario.equity_value },
                      ].map((row) => (
                        <WaterfallBar
                          key={row.label}
                          label={row.label}
                          value={Math.abs(row.value)}
                          max={baseScenario.enterprise_value * 1.1}
                        />
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}

            {/* ────────────── REPORT TAB ────────────── */}
            {activeTab === "report" && (
              <div>
                <div className="overview-header" style={{ marginBottom: 28 }}>
                  <div>
                    <p className="overview-company">{report.company_name}</p>
                    <p className="overview-ticker">Equity Research — Agentic Pipeline Output</p>
                  </div>
                  {rating && baseScenario && (
                    <div
                      className="rating-badge"
                      style={{
                        color: rating.color,
                        borderColor: rating.color,
                        background: rating.bg,
                      }}
                    >
                      {rating.label} · {fmt.price(baseScenario.price_per_share)}
                    </div>
                  )}
                </div>

                {[
                  { label: "Brand & Positioning", text: report.brand_and_positioning },
                  { label: "Financial Highlights", text: report.financial_highlights },
                  { label: "Valuation Summary", text: report.valuation_summary },
                  { label: "Investment Recommendation", text: report.investment_recommendation },
                ].map(({ label, text }) => (
                  <div className="report-section" key={label}>
                    <p className="report-section-label">{label}</p>
                    <p className="report-section-text">{text}</p>
                  </div>
                ))}

                {/* Risk factors */}
                {report.risk_factors?.length > 0 && (
                  <div className="report-section">
                    <p className="report-section-label">Risk Factors</p>
                    {report.risk_factors.map((r, i) => (
                      <div className="risk-row" key={i}>
                        <span
                          className={`risk-badge risk-${r.severity?.toLowerCase() || "medium"}`}
                        >
                          {r.severity || "MED"}
                        </span>
                        <div>
                          <p className="risk-title">{r.title}</p>
                          <p className="risk-desc">{r.description}</p>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* ────────────── ADVISORY TAB ────────────── */}
            {activeTab === "advisory" && (
              <div>
                <p className="advisory-header">Strategic & Funding Advisory</p>
                <p className="advisory-sub">
                  AI-generated strategic options · Treat as discussion framework, not investment
                  advice
                </p>

                {/* Advisory recommendation summary */}
                {rating && baseScenario && (
                  <div
                    style={{
                      background: "var(--surface)",
                      border: `1px solid ${rating.color}30`,
                      borderRadius: 6,
                      padding: 24,
                      marginBottom: 24,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                    }}
                  >
                    <div>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 10,
                          color: "var(--muted)",
                          letterSpacing: "0.15em",
                          textTransform: "uppercase",
                          marginBottom: 8,
                        }}
                      >
                        Advisory Recommendation
                      </p>
                      <div
                        className="rating-badge"
                        style={{
                          color: rating.color,
                          borderColor: rating.color,
                          background: rating.bg,
                          display: "inline-flex",
                        }}
                      >
                        {rating.label}
                      </div>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 10,
                          color: "var(--muted)",
                          marginBottom: 4,
                        }}
                      >
                        12M BASE TARGET
                      </p>
                      <p
                        style={{
                          fontFamily: "Playfair Display, serif",
                          fontSize: 32,
                          color: rating.color,
                        }}
                      >
                        {fmt.price(baseScenario.price_per_share)}
                      </p>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 11,
                          color: rating.color,
                        }}
                      >
                        {fmt.signed(baseScenario.upside_downside_pct)} implied upside
                      </p>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 10,
                          color: "var(--muted)",
                          marginBottom: 8,
                        }}
                      >
                        SCENARIO RANGE
                      </p>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 12,
                          color: "#FF4444",
                        }}
                      >
                        ▼ {fmt.price(downsideScenario?.price_per_share || 0)}
                      </p>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 12,
                          color: "#C8A951",
                        }}
                      >
                        ● {fmt.price(baseScenario.price_per_share)}
                      </p>
                      <p
                        style={{
                          fontFamily: "JetBrains Mono, monospace",
                          fontSize: 12,
                          color: "#00C853",
                        }}
                      >
                        ▲ {fmt.price(upsideScenario?.price_per_share || 0)}
                      </p>
                    </div>
                  </div>
                )}

                {/* Funding options */}
                {report.funding_and_strategic_options?.length > 0 && (
                  <>
                    <p className="section-label">Funding & Strategic Options</p>
                    <div className="advisory-grid">
                      {report.funding_and_strategic_options.map((opt, i) => (
                        <div className="advisory-card" key={i}>
                          <p className="advisory-card-title">{opt.option}</p>
                          <p className="advisory-card-content">{opt.rationale}</p>
                          <div className="pros-cons">
                            <ul className="pros">
                              {opt.pros?.map((p, j) => (
                                <li key={j}>{p}</li>
                              ))}
                            </ul>
                            <ul className="cons">
                              {opt.cons?.map((c, j) => (
                                <li key={j}>{c}</li>
                              ))}
                            </ul>
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                )}

                {/* Full recommendation */}
                <div className="report-section" style={{ marginTop: 8 }}>
                  <p className="report-section-label">Investment Thesis</p>
                  <p className="report-section-text">{report.investment_recommendation}</p>
                </div>
              </div>
            )}
          </div>

          {/* ── Disclaimer ── */}
          <div className="disclaimer fade-up fade-up-3">
            ⚠{" "}
            {report.disclaimer ||
              "This report was generated by an AI system using publicly available data. For educational purposes only. Not investment advice. All DCF outputs are model projections, not forecasts."}
          </div>
        </>
      )}
    </div>
  );
}