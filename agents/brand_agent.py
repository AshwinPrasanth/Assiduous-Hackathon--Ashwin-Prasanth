"""
agents/brand_agent.py
---------------------
Brand & Positioning Agent — criterion 1 of the brief.

Fetches publicly available text about a company:
  1. Investor Relations page (company website)
  2. SEC EDGAR 10-K MD&A section (most recent annual filing)
  3. Recent news headlines via RSS / free NewsAPI

Text is chunked and stored in a simple in-memory vector store (cosine similarity
over TF-IDF vectors — no external vector DB dependency). The report agent then
calls retrieve() to pull relevant passages into its prompts.

This satisfies:
  - Criterion 1: brand & positioning capture from public sources
  - Criterion 5: agentic retrieval (tool-calling pattern)
  - Criterion 6: ingest data from multiple source types
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional

import httpx
import structlog
from bs4 import BeautifulSoup

logger = structlog.get_logger(__name__)

EDGAR_USER_AGENT = "FinSightAI hackathon@example.com"

# ---------------------------------------------------------------------------
# Tiny in-process vector store (TF-IDF cosine similarity — no dependencies)
# ---------------------------------------------------------------------------

class SimpleVectorStore:
    """
    Lightweight RAG store using TF-IDF bag-of-words vectors.
    Sufficient for ~100 chunks. No sklearn, no numpy required.
    """

    def __init__(self):
        self.chunks: list[str] = []
        self.sources: list[str] = []
        self._vocab: list[str] = []
        self._matrix: list[list[float]] = []

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"[a-z]{2,}", text.lower())

    def _tfidf(self, tokens: list[str], doc_freq: Counter) -> dict[str, float]:
        tf = Counter(tokens)
        n = len(self.chunks) or 1
        return {
            word: (count / len(tokens)) * math.log(n / (doc_freq[word] + 1))
            for word, count in tf.items()
            if word in self._vocab
        }

    def build(self):
        """Build vocabulary and TF-IDF matrix from stored chunks."""
        if not self.chunks:
            return
        tokenized = [self._tokenize(c) for c in self.chunks]
        doc_freq: Counter = Counter()
        for toks in tokenized:
            doc_freq.update(set(toks))

        # Vocabulary: top-2000 words by document frequency
        self._vocab = [w for w, _ in doc_freq.most_common(2000)]
        vocab_set = set(self._vocab)

        n = len(self.chunks)
        self._matrix = []
        for toks in tokenized:
            tf = Counter(toks)
            vec = [
                (tf.get(w, 0) / max(len(toks), 1)) * math.log(n / (doc_freq[w] + 1))
                for w in self._vocab
            ]
            self._matrix.append(vec)

    def _cosine(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb + 1e-10)

    def add(self, text: str, source: str):
        self.chunks.append(text)
        self.sources.append(source)

    def retrieve(self, query: str, k: int = 4) -> list[dict]:
        """Return top-k most relevant chunks for a query."""
        if not self._matrix:
            self.build()
        if not self._matrix:
            return []

        toks = self._tokenize(query)
        if not toks:
            return []

        # Build query vector (TF only — simplified)
        tf = Counter(toks)
        q_vec = [tf.get(w, 0) / max(len(toks), 1) for w in self._vocab]

        scores = [(self._cosine(q_vec, doc_vec), i) for i, doc_vec in enumerate(self._matrix)]
        scores.sort(reverse=True)

        return [
            {"text": self.chunks[i], "source": self.sources[i], "score": score}
            for score, i in scores[:k]
            if score > 0.01
        ]


# Global per-ticker store
_stores: dict[str, SimpleVectorStore] = {}


def get_store(ticker: str) -> SimpleVectorStore:
    if ticker not in _stores:
        _stores[ticker] = SimpleVectorStore()
    return _stores[ticker]


def retrieve_brand_context(ticker: str, query: str, k: int = 4) -> str:
    """
    Public function called by report agent to get relevant brand/IR/news context.
    Returns formatted string ready for LLM prompt injection.
    """
    store = _stores.get(ticker)
    if not store or not store.chunks:
        return ""
    results = store.retrieve(query, k=k)
    if not results:
        return ""
    lines = [f"[Source: {r['source']}]\n{r['text']}" for r in results]
    return "\n\n---\n\n".join(lines)


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

async def _fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch a URL and return cleaned text content."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FinSightBot/1.0; research use)",
                "Accept": "text/html,application/xhtml+xml",
            },
            timeout=timeout,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "lxml")
            # Remove nav, footer, scripts
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            # Collapse whitespace
            text = re.sub(r"\s+", " ", text)
            return text[:50_000]  # cap at 50k chars
    except Exception as exc:
        logger.warning("fetch_html_failed", url=url, error=str(exc))
        return None


def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 80) -> list[str]:
    """Split text into overlapping word-level chunks."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


# ---------------------------------------------------------------------------
# Source 1: Company IR / About page
# ---------------------------------------------------------------------------

# Known IR page patterns for major tickers
_IR_OVERRIDES = {
    "AAPL": "https://investor.apple.com/investor-relations/overview/default.aspx",
    "MSFT": "https://www.microsoft.com/en-us/investor",
    "GOOGL": "https://abc.xyz/investor/",
    "GOOG": "https://abc.xyz/investor/",
    "NVDA": "https://investor.nvidia.com/home/default.aspx",
    "META": "https://investor.fb.com/home/default.aspx",
    "AMZN": "https://ir.aboutamazon.com/overview/default.aspx",
    "TSLA": "https://ir.tesla.com/",
}


async def _scrape_ir_page(ticker: str, website: str) -> list[str]:
    """Fetch the IR / about page and return text chunks."""
    url = _IR_OVERRIDES.get(ticker.upper())
    if not url and website:
        # Try /investor-relations and /about as common patterns
        base = website.rstrip("/")
        url = f"{base}/investor-relations"

    if not url:
        logger.info("ir_page_skipped", ticker=ticker, reason="no_url")
        return []

    text = await _fetch_html(url)
    if not text:
        # Fallback to main website
        if website:
            text = await _fetch_html(website)
    if not text:
        return []

    chunks = _chunk_text(text)
    logger.info("ir_page_scraped", ticker=ticker, url=url, chunks=len(chunks))
    return [f"[IR Page] {c}" for c in chunks[:20]]  # cap at 20 chunks


# ---------------------------------------------------------------------------
# Source 2: SEC EDGAR 10-K MD&A section
# ---------------------------------------------------------------------------

async def _fetch_mda_from_edgar(ticker: str) -> list[str]:
    """
    Pull the most recent 10-K filing index from EDGAR and extract
    the MD&A section text. Returns text chunks.
    """
    try:
        # Resolve CIK
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": EDGAR_USER_AGENT},
            )
            resp.raise_for_status()
            cik = None
            for entry in resp.json().values():
                if entry.get("ticker", "").upper() == ticker.upper():
                    cik = str(entry["cik_str"]).zfill(10)
                    break
            if not cik:
                return []

        # Get submission history to find latest 10-K
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                headers={"User-Agent": EDGAR_USER_AGENT},
            )
            resp.raise_for_status()
            submissions = resp.json()

        filings = submissions.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        docs = filings.get("primaryDocument", [])

        # Find latest 10-K
        ten_k_idx = next((i for i, f in enumerate(forms) if f == "10-K"), None)
        if ten_k_idx is None:
            return []

        accession = accessions[ten_k_idx].replace("-", "")
        primary_doc = docs[ten_k_idx]
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary_doc}"

        text = await _fetch_html(filing_url, timeout=30)
        if not text:
            return []

        # Extract MD&A section heuristically
        mda_match = re.search(
            r"(management.{0,30}discussion.{0,30}analysis)(.*?)(quantitative.{0,30}qualitative|risk factor|financial statement)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if mda_match:
            mda_text = mda_match.group(2)[:15_000]
        else:
            # Fall back to first 15k chars of the document
            mda_text = text[:15_000]

        chunks = _chunk_text(mda_text, chunk_size=300, overlap=60)
        logger.info("mda_fetched", ticker=ticker, cik=cik, chunks=len(chunks))
        return [f"[10-K MD&A] {c}" for c in chunks[:30]]

    except Exception as exc:
        logger.warning("mda_fetch_failed", ticker=ticker, error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Source 3: News headlines via RSS
# ---------------------------------------------------------------------------

async def _fetch_news_rss(ticker: str, company_name: str) -> list[str]:
    """Fetch recent news headlines from Google News RSS (free, no API key)."""
    query = f"{ticker} {company_name} earnings revenue".replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "FinSightBot/1.0"},
            timeout=10,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []

        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.find_all("item")[:15]
        headlines = []
        for item in items:
            title = item.find("title")
            pub_date = item.find("pubdate")
            if title:
                date_str = pub_date.text[:16] if pub_date else ""
                headlines.append(f"[News {date_str}] {title.text}")

        logger.info("news_fetched", ticker=ticker, count=len(headlines))
        return headlines

    except Exception as exc:
        logger.warning("news_fetch_failed", ticker=ticker, error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_brand_agent(ticker: str, company_name: str, website: str) -> dict:
    """
    Run all three brand data fetchers, chunk the results,
    and store in the per-ticker vector store.

    Returns a summary dict for logging/display.
    """
    logger.info("brand_agent_start", ticker=ticker)
    store = get_store(ticker)
    store.chunks.clear()
    store.sources.clear()
    store._matrix.clear()

    results = {"ir_chunks": 0, "mda_chunks": 0, "news_items": 0}

    # 1. IR page
    ir_chunks = await _scrape_ir_page(ticker, website)
    for c in ir_chunks:
        store.add(c, "IR Page")
    results["ir_chunks"] = len(ir_chunks)

    # 2. MD&A from 10-K
    mda_chunks = await _fetch_mda_from_edgar(ticker)
    for c in mda_chunks:
        store.add(c, "10-K MD&A")
    results["mda_chunks"] = len(mda_chunks)

    # 3. News RSS
    news_items = await _fetch_news_rss(ticker, company_name)
    for item in news_items:
        store.add(item, "News")
    results["news_items"] = len(news_items)

    # Build the vector index
    store.build()

    total = len(store.chunks)
    logger.info(
        "brand_agent_complete",
        ticker=ticker,
        total_chunks=total,
        **results,
    )
    return results