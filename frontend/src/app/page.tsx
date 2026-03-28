"use client";

import { useState } from 'react';

export default function Home() {
  const [ticker, setTicker] = useState('');
  const [logs, setLogs] = useState<string[]>([]);
  const [status, setStatus] = useState('idle'); // idle, running, complete, failed
  const [report, setReport] = useState<any>(null);

  // CRITICAL: Replace this string with your ACTUAL Port 8000 URL from the "Ports" tab
  const API_BASE = "https://literate-robot-g94vxv944v9hpwr5-8000.app.github.dev";

  const startAnalysis = async () => {
    if (!ticker) return;
    
    // UI Reset
    setLogs(["[system] Initializing multi-agent pipeline...", "[system] Connecting to Groq LPU..."]);
    setStatus('running');
    setReport(null);

    try {
      // 1. Trigger the Real AI Analysis
      // We are using a standard POST. Because it's a 70B model, 
      // this might take 30-45 seconds. The browser will wait.
      const response = await fetch(`${API_BASE}/api/pipeline/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker: ticker.toUpperCase() })
      });

      if (!response.ok) throw new Error('Analysis Engine Timeout');

      setLogs(prev => [...prev, "[agent] Research Complete.", "[system] Synthesizing Final Report..."]);

      // 2. Fetch the Resulting Report
      const reportRes = await fetch(`${API_BASE}/api/report/${ticker.toUpperCase()}`);
      const reportData = await reportRes.json();
      
      if (reportData) {
        setReport(reportData);
        setStatus('complete');
        setLogs(prev => [...prev, "✓ Portfolio-grade analysis generated."]);
      }

    } catch (err) {
      console.error("Frontend Error:", err);
      setStatus('failed');
      setLogs(prev => [...prev, "✗ Error: Connection to AI agents lost. Check Backend Terminal."]);
    }
  };

  return (
    <main className="min-h-screen bg-[#0A0A0F] text-[#F5F3EE] p-8 font-sans">
      <div className="max-w-4xl mx-auto space-y-12">
        <header className="border-b border-white/10 pb-6">
          <h1 className="text-5xl font-serif font-bold text-[#C8A951] mb-2 text-shadow-glow">FinSight</h1>
          <p className="text-[#8888A0] text-sm uppercase tracking-[0.2em] font-medium italic">Corporate Finance Autopilot</p>
        </header>

        {/* Search Bar */}
        <div className="relative group">
          <input 
            className="w-full bg-transparent border-b-2 border-white/10 py-4 text-4xl uppercase font-serif focus:border-[#C8A951] transition-all duration-500 outline-none placeholder:opacity-20"
            placeholder="ENTER TICKER (e.g. NVDA)"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && startAnalysis()}
          />
          <button 
            onClick={startAnalysis}
            disabled={status === 'running'}
            className="absolute right-0 bottom-4 text-[#C8A951] font-bold uppercase tracking-widest hover:text-white transition-all disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {status === 'running' ? 'Agents Thinking...' : 'Analyze →'}
          </button>
        </div>

        {/* Live Terminal Output */}
        <div className="bg-black/60 rounded-lg p-6 font-mono text-xs border border-white/10 h-64 overflow-y-auto shadow-inner">
          {logs.map((log, i) => (
            <div key={i} className="mb-2 flex gap-3">
              <span className="opacity-20 shrink-0">{new Date().toLocaleTimeString([], {hour12: false})}</span>
              <span className={log.includes('✗') ? 'text-red-400' : log.includes('✓') ? 'text-green-400' : 'text-[#8888A0]'}>
                {log}
              </span>
            </div>
          ))}
          {status === 'running' && <div className="animate-pulse text-[#C8A951] ml-12">▋</div>}
        </div>

        {/* The Final AI Report */}
        {report && (
          <div className="animate-in fade-in slide-in-from-bottom-8 duration-1000 bg-[#F5F3EE] text-[#0A0A0F] p-10 rounded-lg shadow-2xl space-y-8 border-t-4 border-[#C8A951]">
            <div className="flex justify-between items-end border-b border-black/10 pb-6">
              <h2 className="text-5xl font-serif italic">{report.company_name || ticker}</h2>
              <span className="text-xs font-bold tracking-tighter opacity-50 uppercase">Equity Research Memo // Confidential</span>
            </div>
            
            <div className="grid grid-cols-1 md:grid-cols-2 gap-12">
              <section>
                <h3 className="text-[#8B7235] text-xs font-bold uppercase tracking-[0.2em] mb-4 border-l-2 border-[#C8A951] pl-3">Executive Summary</h3>
                <p className="leading-relaxed font-serif text-lg italic opacity-90">
                  {report.executive_summary || "Synthetic reasoning in progress..."}
                </p>
              </section>
              
              <section className="bg-black/5 p-6 rounded-md">
                <h3 className="text-[#8B7235] text-xs font-bold uppercase tracking-[0.2em] mb-4">Valuation Logic</h3>
                <p className="text-sm leading-relaxed opacity-80">
                  {report.valuation_summary || "Calculated using 5-year DCF via multi-scenario Llama 3 analysis."}
                </p>
              </section>
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
