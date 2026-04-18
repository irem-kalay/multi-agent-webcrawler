"""Web dashboard and HTTP API server for the concurrent web crawler.

Provides:
* Single-page HTML dashboard (GET /)
* POST /api/start   — seed a new crawl (seed_url + max_depth)
* GET  /api/metrics — live JSON metrics (visited count, queue depth, back-pressure)
* POST /api/search  — run a query and return ranked results
* GET  /api/status  — simple health-check / crawl-running flag
* POST /api/stop    — graceful shutdown (saves state via CrawlerState.save_state)

Only Python built-ins are used: http.server, json, signal, threading, urllib.
No Flask, FastAPI, or any third-party framework.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse
import queue

# ---------------------------------------------------------------------------
# Local modules (must live in the same directory or be on PYTHONPATH)
# ---------------------------------------------------------------------------
from data_structures import CrawlTask, CrawlQueue, CrawlerState
from parser import start_workers
from search_engine import SearchEngine

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global crawler state (initialised once, shared with every request handler)
# ---------------------------------------------------------------------------
_state: CrawlerState = CrawlerState(crawl_queue=CrawlQueue(maxsize=500))
_workers: List[threading.Thread] = []
_crawl_running: bool = False
_workers_lock = threading.Lock()

# Default server configuration
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 3600
NUM_WORKERS = 4
STATE_FILE = "crawler_state.json"

# Back-pressure threshold: queue is considered "under pressure" when >80% full
BACKPRESSURE_RATIO = 0.80

# ---------------------------------------------------------------------------
# Recent-crawl session history
# ---------------------------------------------------------------------------
_sessions: List[Dict[str, Any]] = []
_sessions_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    """Send a JSON-encoded response."""
    body = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    """Read and JSON-decode the request body."""
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _metrics() -> Dict[str, Any]:
    """Compute current crawler metrics without holding locks longer than needed."""
    visited_count = len(_state.visited)
    queue_depth = _state.crawl_queue.qsize()
    maxsize = _state.crawl_queue.maxsize
    back_pressure = (
        (queue_depth / maxsize) >= BACKPRESSURE_RATIO
        if maxsize > 0
        else False
    )
    # Flip any "Active" sessions to "Finished" when the queue is empty
    with _sessions_lock:
        if queue_depth == 0:
            for session in _sessions:
                if session["status"] == "Active":
                    session["status"] = "Finished"
        sessions_snapshot = list(_sessions)

    return {
        "visited_urls": visited_count,
        "queue_depth": queue_depth,
        "queue_maxsize": maxsize,
        "back_pressure": back_pressure,
        "back_pressure_pct": round((queue_depth / maxsize * 100) if maxsize > 0 else 0, 1),
        "crawl_running": _crawl_running,
        "indexed_terms": len(_state.inverted_index.snapshot()),
        "sessions": sessions_snapshot,
    }


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class CrawlerHandler(BaseHTTPRequestHandler):
    """Handle all inbound HTTP requests for the crawler dashboard."""

    # Suppress default request logging (we do our own)
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        logger.debug("HTTP %s", fmt % args)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        
        if path == "/" or path == "/index.html":
            self._serve_dashboard()
        elif path == "/api/metrics":
            _json_response(self, 200, _metrics())
        elif path == "/api/status":
            _json_response(self, 200, {"status": "ok", "crawl_running": _crawl_running})
        elif path == "/search":
            self._handle_search_get(query)
        else:
            _json_response(self, 404, {"error": f"Unknown route: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/start":
            self._handle_start()
        elif path == "/api/stop":
            self._handle_stop()
        else:
            _json_response(self, 404, {"error": f"Unknown route: {path}"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        """Handle CORS pre-flight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ------------------------------------------------------------------
    # Route implementations
    # ------------------------------------------------------------------

    def _handle_start(self) -> None:
        """POST /api/start — seed a new crawl."""
        global _crawl_running, _workers

        try:
            body = _read_body(self)
            seed_url: str = body.get("seed_url", "").strip()
            max_depth: int = int(body.get("max_depth", 2))
            capacity: int = int(body.get("capacity", 500))
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            _json_response(self, 400, {"error": f"Invalid request body: {exc}"})
            return

        if not seed_url:
            _json_response(self, 400, {"error": "seed_url is required."})
            return
        if not (seed_url.startswith("http://") or seed_url.startswith("https://")):
            _json_response(self, 400, {"error": "seed_url must start with http:// or https://"})
            return
        if max_depth < 0 or max_depth > 10:
            _json_response(self, 400, {"error": "max_depth must be between 0 and 10."})
            return
        if capacity < 1 or capacity > 10000:
            _json_response(self, 400, {"error": "capacity must be between 1 and 10000."})
            return

        with _workers_lock:
            # Dynamically set the queue capacity
            try:
                _state.crawl_queue.set_maxsize(capacity)
                logger.info("Queue capacity set to %d", capacity)
            except ValueError as e:
                _json_response(self, 400, {"error": f"Cannot change capacity: {str(e)}"})
                return

            # Mark seed URL as visited and record its metadata
            if _state.visited.mark_visited(seed_url):
                _state.metadata_map.record(
                    url=seed_url,
                    origin_url=None,
                    depth=0,
                )

            seed_task = CrawlTask(
                url=seed_url,
                depth=0,
                origin_url=None,
                max_depth=max_depth,
            )

            try:
                # Wait up to 2 seconds for space to free up
                _state.crawl_queue.add_task(seed_task, block=True, timeout=2.0)
            except queue.Full:
                _json_response(self, 429, {"error": "Queue is completely full! Please wait for workers to finish current pages."})
                return
            except Exception as e:
                _json_response(self, 500, {"error": f"Failed to enqueue: {str(e)}"})
                return
            # Start workers only if not already running
            if not _crawl_running:
                _workers = start_workers(
                    num_workers=NUM_WORKERS,
                    crawl_queue=_state.crawl_queue,
                    visited_set=_state.visited,
                    inverted_index=_state.inverted_index,
                    metadata_map=_state.metadata_map,
                )
                _crawl_running = True
                logger.info("Crawl started: seed=%s depth=%d capacity=%d workers=%d", seed_url, max_depth, capacity, NUM_WORKERS)

            # Record this crawl in the session history
            import datetime as _dt
            with _sessions_lock:
                _sessions.append({
                    "seed_url": seed_url,
                    "status": "Active",
                    "time": _dt.datetime.now().strftime("%H:%M:%S"),
                })

        _json_response(self, 200, {
            "message": "Crawl initiated.",
            "seed_url": seed_url,
            "max_depth": max_depth,
            "capacity": capacity,
            "workers": NUM_WORKERS,
        })

    def _handle_search_get(self, query_params: Dict[str, List[str]]) -> None:
        """GET /search?query=<word>&sortBy=relevance — run a query against the inverted index."""
        try:
            # Extract query parameter from URL query string
            query_list = query_params.get("query", [])
            if not query_list or not query_list[0].strip():
                _json_response(self, 400, {"error": "query parameter is required."})
                return
            query: str = query_list[0].strip()
            
            # Extract sortBy parameter (currently only supports 'relevance')
            sort_by_list = query_params.get("sortBy", ["relevance"])
            sort_by: str = sort_by_list[0] if sort_by_list else "relevance"
            
            max_results: int = 20
            if "maxResults" in query_params:
                try:
                    max_results = int(query_params["maxResults"][0])
                except (ValueError, IndexError):
                    pass
        except Exception as exc:
            _json_response(self, 400, {"error": f"Invalid query parameters: {exc}"})
            return

        if not query:
            _json_response(self, 400, {"error": "query is required."})
            return

        engine = SearchEngine(_state.inverted_index, _state.metadata_map)
        results = engine.search(query, max_results=max_results)

        serialized = [
            {
                "url": r.url,
                "origin_url": r.origin_url,
                "depth": r.depth,
                "relevance_score": r.relevance_score,
                "frequency": r.frequency,
            }
            for r in results
        ]
        _json_response(self, 200, {
            "query": query,
            "total": len(serialized),
            "results": serialized,
        })

    def _handle_stop(self) -> None:
        """POST /api/stop — request a graceful shutdown."""
        _json_response(self, 200, {"message": "Shutdown initiated. State will be saved."})
        # Fire shutdown in a background thread so the response can be sent first
        threading.Thread(target=_graceful_shutdown, daemon=True).start()

    # ------------------------------------------------------------------
    # Dashboard HTML
    # ------------------------------------------------------------------

    def _serve_dashboard(self) -> None:
        html = _DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_server_instance: HTTPServer | None = None


def _graceful_shutdown(*_: Any) -> None:
    """Stop the HTTP server and save crawler state before exiting."""
    logger.info("Shutdown requested — saving state to %s …", STATE_FILE)
    try:
        saved = _state.save_state(STATE_FILE)
        logger.info("State saved to %s", saved)
    except Exception as exc:
        logger.error("Failed to save state: %s", exc)

    # Save index to p.data file
    logger.info("Saving inverted index to data/storage/p.data …")
    try:
        saved_index = _state.inverted_index.save_to_file("data/storage/p.data", _state.metadata_map)
        logger.info("Index saved to %s", saved_index)
    except Exception as exc:
        logger.error("Failed to save index to p.data: %s", exc)

    if _server_instance is not None:
        logger.info("Shutting down HTTP server …")
        threading.Thread(target=_server_instance.shutdown, daemon=True).start()

    logger.info("Goodbye.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Embedded single-page dashboard — Google-in-a-Day light-mode UI
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Google-in-a-Day Search</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * {
      box-sizing: border-box;
      -webkit-font-smoothing: antialiased;
    }
    body {
      margin: 0;
      padding: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: #ffffff;
      color: #202124;
    }
    a { text-decoration: none; color: inherit; }

    /* ── Header ── */
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      background: #ffffff;
      border-bottom: 1px solid #dadce0;
      padding: 14px 32px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .logo { font-size: 20px; font-weight: 600; letter-spacing: 0.02em; }
    .logo span:nth-child(1) { color: #4285f4; }
    .logo span:nth-child(2) { color: #ea4335; }
    .logo span:nth-child(3) { color: #fbbc05; }
    .logo span:nth-child(4) { color: #4285f4; }
    .logo span:nth-child(5) { color: #34a853; }
    .logo span:nth-child(6) { color: #ea4335; }
    .header-right {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    #statusText { font-size: 12px; color: #5f6368; }
    #statusDot {
      width: 9px; height: 9px;
      border-radius: 50%;
      background: #9aa0a6;
      display: inline-block;
    }
    #statusDot.running { background: #34a853; animation: blink 1.4s infinite; }
    #statusDot.idle    { background: #9aa0a6; }
    @keyframes blink { 50% { opacity: 0.3; } }
    .btn-stop {
      border: 1px solid #dadce0;
      background: #fff;
      color: #c5221f;
      font-size: 12px;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 4px;
      cursor: pointer;
      transition: background 0.2s, box-shadow 0.2s;
    }
    .btn-stop:hover { background: #fce8e6; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }
    .btn-stop:active { transform: translateY(1px); }

    /* ── Page layout ── */
    .page-shell {
      max-width: 1200px;
      margin: 24px auto 40px auto;
      padding: 0 32px;
    }
    @media (max-width: 960px) { .page-shell { padding: 0 16px; } }
    .content-grid {
      display: grid;
      grid-template-columns: minmax(0, 3fr) minmax(280px, 2fr);
      column-gap: 32px;
      align-items: flex-start;
    }
    @media (max-width: 960px) {
      .content-grid { grid-template-columns: minmax(0, 1fr); row-gap: 24px; }
    }

    /* ── Search panel ── */
    .search-panel { padding-right: 8px; }
    .search-form-shell { margin-bottom: 24px; }
    .search-form-wrapper { display: flex; align-items: center; max-width: 700px; }
    .search-bar {
      display: flex;
      align-items: center;
      flex: 1;
      border-radius: 999px;
      border: 1px solid #dfe1e5;
      background: #ffffff;
      padding: 8px 14px;
      transition: box-shadow 0.2s ease, border-color 0.2s ease;
    }
    .search-bar:hover,
    .search-bar:focus-within {
      box-shadow: 0 1px 6px rgba(32,33,36,0.28);
      border-color: rgba(223,225,229,0);
    }
    .search-bar input[type="text"] {
      flex: 1;
      border: none;
      outline: none;
      font-size: 16px;
      padding: 4px 6px;
      color: #202124;
      background: transparent;
    }
    .search-bar input[type="text"]::placeholder { color: #9aa0a6; }
    .search-bar button {
      border: 1px solid #dadce0;
      background: #f8f9fa;
      color: #3c4043;
      font-size: 14px;
      padding: 6px 14px;
      margin-left: 8px;
      border-radius: 4px;
      cursor: pointer;
      transition: background 0.2s, box-shadow 0.2s, transform 0.1s;
    }
    .search-bar button:hover { border-color: #c6c6c6; box-shadow: 0 1px 1px rgba(0,0,0,0.1); }
    .search-bar button:active { transform: translateY(1px); box-shadow: none; }
    .search-hint { margin-top: 12px; font-size: 13px; color: #5f6368; }

    /* ── Results ── */
    .results-header { font-size: 13px; color: #5f6368; margin-bottom: 6px; }
    .results-list { list-style: none; padding: 0; margin: 0; }
    .result-item { margin-bottom: 24px; max-width: 680px; }
    .result-item:last-child { margin-bottom: 0; }
    .result-link { font-size: 18px; line-height: 1.3; color: #1a0dab; }
    .result-link:hover { text-decoration: underline; }
    .result-display-url { font-size: 14px; color: #4d5156; margin-top: 2px; word-break: break-all; }
    .result-meta { font-size: 13px; color: #5f6368; margin-top: 4px; }
    .muted { font-size: 13px; color: #5f6368; }

    /* ── Dashboard sidebar ── */
    .dashboard-panel {
      border: 1px solid #dadce0;
      border-radius: 8px;
      padding: 16px 18px 18px 18px;
      background: #f8f9fa;
    }
    .dashboard-title { margin: 0 0 4px 0; font-size: 16px; font-weight: 500; color: #202124; }
    .dashboard-subtitle { margin: 0 0 14px 0; font-size: 13px; color: #5f6368; }

    /* Metrics */
    .metrics-grid { display: grid; grid-template-columns: 1fr; row-gap: 10px; }
    .metric-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 10px;
      border-radius: 6px;
      background: #ffffff;
      border: 1px solid #e0e0e0;
    }
    .metric-label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: #5f6368; }
    .metric-value { font-size: 14px; font-weight: 500; color: #202124; display: flex; align-items: center; gap: 4px; }

    /* Back-pressure pill */
    .metric-pill {
      display: inline-flex;
      align-items: center;
      padding: 2px 8px 2px 6px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 500;
      border: 1px solid #dadce0;
      background: #ffffff;
      color: #5f6368;
    }
    .metric-pill::before {
      content: "";
      width: 8px; height: 8px;
      border-radius: 50%;
      background: #9aa0a6;
      margin-right: 5px;
    }
    .metric-pill.ok    { color: #137333; border-color: #cce8d8; background: #e6f4ea; }
    .metric-pill.ok::before    { background: #34a853; }
    .metric-pill.high  { color: #c5221f; border-color: #fad2cf; background: #fce8e6; }
    .metric-pill.high::before  { background: #ea4335; animation: blink 1s infinite; }

    /* Queue progress bar */
    .queue-bar-wrap {
      margin-top: 4px;
      height: 5px;
      background: #e0e0e0;
      border-radius: 3px;
      overflow: hidden;
    }
    .queue-bar {
      height: 100%;
      border-radius: 3px;
      background: #34a853;
      transition: width 0.4s ease, background 0.4s ease;
    }

    .metrics-footnote { margin-top: 12px; font-size: 12px; color: #5f6368; line-height: 1.5; }

    /* Start Indexing section */
    .indexing-section {
      margin-top: 18px;
      padding-top: 14px;
      border-top: 1px solid #e0e0e0;
    }
    .indexing-title { margin: 0 0 4px 0; font-size: 13px; font-weight: 500; color: #202124; }
    .indexing-description { margin: 0 0 10px 0; font-size: 12px; color: #5f6368; }
    .indexing-form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .indexing-form input[type="url"] {
      flex: 1;
      min-width: 160px;
      border-radius: 999px;
      border: 1px solid #dfe1e5;
      padding: 6px 10px;
      font-size: 13px;
      outline: none;
      background: #fff;
    }
    .depth-input-wrap { display: flex; align-items: center; gap: 6px; }
    .depth-input-wrap label { font-size: 12px; color: #5f6368; white-space: nowrap; }
    .indexing-form input[type="number"] {
      width: 52px;
      border-radius: 6px;
      border: 1px solid #dfe1e5;
      padding: 6px 8px;
      font-size: 13px;
      outline: none;
      text-align: center;
      background: #fff;
    }
    .indexing-form button {
      border: none;
      background: #1a73e8;
      color: #ffffff;
      font-size: 13px;
      padding: 6px 14px;
      border-radius: 4px;
      cursor: pointer;
      transition: background 0.2s, box-shadow 0.2s, transform 0.1s;
    }
    .indexing-form button:hover { background: #185abc; box-shadow: 0 1px 2px rgba(0,0,0,0.15); }
    .indexing-form button:active { transform: translateY(1px); }
    .indexing-status { margin-top: 6px; font-size: 12px; color: #5f6368; }

    /* Activity log */
    .log-section { margin-top: 18px; padding-top: 14px; border-top: 1px solid #e0e0e0; }
    .log-title { margin: 0 0 6px 0; font-size: 13px; font-weight: 500; color: #202124; }
    .log-box {
      background: #ffffff;
      border: 1px solid #e0e0e0;
      border-radius: 6px;
      font-family: "Roboto Mono", Menlo, Consolas, monospace;
      font-size: 11px;
      color: #5f6368;
      padding: 8px 10px;
      height: 120px;
      overflow-y: auto;
      line-height: 1.8;
    }
    .log-info  { color: #137333; }
    .log-warn  { color: #b06000; }
    .log-error { color: #c5221f; }
    .log-ts    { color: #9aa0a6; margin-right: 6px; }

    /* ── Recent Crawls ── */
    .recent-crawls-section {
      margin-top: 18px;
      padding-top: 14px;
      border-top: 1px solid #e0e0e0;
    }
    .recent-crawls-title { margin: 0 0 8px 0; font-size: 13px; font-weight: 500; color: #202124; }
    .recent-crawls-list { display: flex; flex-direction: column; gap: 6px; }
    .recent-crawl-item {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      background: #ffffff;
      border: 1px solid #e0e0e0;
      border-radius: 6px;
      font-size: 12px;
      overflow: hidden;
    }
    .recent-crawl-url {
      flex: 1;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: #202124;
    }
    .recent-crawl-time { color: #9aa0a6; white-space: nowrap; flex-shrink: 0; }
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .status-badge.active {
      background: #e6f4ea;
      color: #137333;
      border: 1px solid #cce8d8;
    }
    .status-badge.active::before {
      content: "";
      width: 6px; height: 6px;
      border-radius: 50%;
      background: #34a853;
      animation: blink 1.4s infinite;
    }
    .status-badge.finished {
      background: #f1f3f4;
      color: #5f6368;
      border: 1px solid #e0e0e0;
    }
    .status-badge.finished::before {
      content: "";
      width: 6px; height: 6px;
      border-radius: 50%;
      background: #9aa0a6;
    }
    .recent-crawls-empty { font-size: 12px; color: #9aa0a6; font-style: italic; }

    /* Footer */
    footer {
      padding: 12px 32px 18px 32px;
      border-top: 1px solid #dadce0;
      font-size: 12px;
      color: #5f6368;
      background: #f8f9fa;
    }
    code { font-family: "Roboto Mono", Menlo, Consolas, monospace; font-size: 12px; color: #202124; }
  </style>
</head>
<body>

<header>
  <div class="logo">
    <span>G</span><span>o</span><span>o</span><span>g</span><span>l</span><span>e</span>-in-a-Day
  </div>
  <div class="header-right">
    <div style="display:flex;align-items:center;gap:6px;">
      <span id="statusDot" class="idle"></span>
      <span id="statusText">Idle</span>
    </div>
    <button class="btn-stop" onclick="stopServer()">⏹ Stop &amp; Save</button>
  </div>
</header>

<main class="page-shell">
  <div class="content-grid">

    <!-- Left: Search -->
    <section class="search-panel">
      <div class="search-form-shell">
        <form id="searchForm" class="search-form-wrapper">
          <div class="search-bar">
            <input
              id="searchInput"
              type="text"
              placeholder="Type a search term and press Enter…"
              autocomplete="off"
            />
            <button type="submit">Search</button>
          </div>
        </form>
        <p id="searchInfo" class="search-hint">Submit a query to see results. Start a crawl first using the panel on the right.</p>
      </div>
      <div>
        <p class="results-header">Search results</p>
        <ul id="results" class="results-list"></ul>
      </div>
    </section>

    <!-- Right: Dashboard -->
    <aside class="dashboard-panel">
      <h2 class="dashboard-title">Crawler Dashboard</h2>
      <p class="dashboard-subtitle">Monitor the crawler state in real time.</p>

      <div class="metrics-grid">
        <div class="metric-row">
          <div class="metric-label">Visited URLs</div>
          <div class="metric-value" id="mVisited">—</div>
        </div>
        <div class="metric-row">
          <div class="metric-label">Indexed Terms</div>
          <div class="metric-value" id="mIndexed">—</div>
        </div>
        <div class="metric-row">
          <div class="metric-label">Queue Depth</div>
          <div class="metric-value">
            <span id="mQueueDepth">—</span>
            <span id="mQueueMax" class="muted">/ —</span>
          </div>
        </div>
        <div class="metric-row" style="flex-direction:column;align-items:flex-start;gap:4px;">
          <div style="display:flex;justify-content:space-between;width:100%;">
            <div class="metric-label">Queue Fill</div>
            <span id="bpPill" class="metric-pill">—</span>
          </div>
          <div class="queue-bar-wrap" style="width:100%;">
            <div class="queue-bar" id="queueBar" style="width:0%;"></div>
          </div>
        </div>
      </div>

      <p class="metrics-footnote">
        Metrics refresh every 2 s from <code>/api/metrics</code>.
        Search uses <code>POST /api/search</code>.
      </p>

      <!-- Start Indexing -->
      <div class="indexing-section">
        <h3 class="indexing-title">Start New Crawl</h3>
        <p class="indexing-description">
          Enter a seed URL and crawl depth to begin indexing.
        </p>
        <form id="indexForm" class="indexing-form">
          <input
            id="indexUrlInput"
            type="url"
            placeholder="https://example.com/"
            autocomplete="off"
            required
          />
          <div class="depth-input-wrap">
            <label for="indexDepthInput">Depth</label>
            <input
              id="indexDepthInput"
              type="number"
              min="0"
              max="10"
              value="2"
              title="Max crawl depth (0–10)"
              autocomplete="off"
            />
          </div>
          <div class="depth-input-wrap">
            <label for="indexCapacityInput">Capacity</label>
            <input
              id="indexCapacityInput"
              type="number"
              min="1"
              max="10000"
              value="500"
              title="Queue capacity (1–10000)"
              autocomplete="off"
            />
          </div>
          <button type="submit">▶ Start Crawl</button>
        </form>
        <p id="indexStatus" class="indexing-status"></p>
      </div>

      <!-- Recent Crawls -->
      <div class="recent-crawls-section">
        <h3 class="recent-crawls-title">Recent Crawls</h3>
        <div id="recentCrawlers" class="recent-crawls-list">
          <span class="recent-crawls-empty">No crawls started yet.</span>
        </div>
      </div>

      <!-- Activity Log -->
      <div class="log-section">
        <h3 class="log-title">Activity Log</h3>
        <div class="log-box" id="logBox"></div>
      </div>
    </aside>

  </div>
</main>

<footer>
  This demo runs entirely on Python's standard library
  (<code>http.server</code>, <code>threading</code>, <code>urllib</code>).
</footer>

<script>
  // ── Utilities ─────────────────────────────────────────────────────────────
  function escHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function ts() {
    return new Date().toTimeString().slice(0, 8);
  }

  function log(msg, type = "info") {
    const box = document.getElementById("logBox");
    const line = document.createElement("div");
    line.innerHTML = `<span class="log-ts">${ts()}</span><span class="log-${type}">${escHtml(msg)}</span>`;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    while (box.children.length > 200) box.removeChild(box.firstChild);
  }

  async function apiFetch(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    return res.json();
  }

  // ── Live metrics polling (every 2 s) ──────────────────────────────────────
  async function pollMetrics() {
    try {
      const m = await apiFetch("GET", "/api/metrics");

      document.getElementById("mVisited").textContent    = (m.visited_urls   ?? 0).toLocaleString();
      document.getElementById("mIndexed").textContent    = (m.indexed_terms  ?? 0).toLocaleString();
      document.getElementById("mQueueDepth").textContent = (m.queue_depth    ?? 0).toLocaleString();
      document.getElementById("mQueueMax").textContent   =
        m.queue_maxsize > 0 ? "/ " + m.queue_maxsize.toLocaleString() : "";

      // Queue progress bar
      const pct = m.queue_maxsize > 0
        ? Math.min(100, (m.queue_depth / m.queue_maxsize) * 100)
        : 0;
      const bar = document.getElementById("queueBar");
      bar.style.width = pct + "%";
      bar.style.background = m.back_pressure ? "#ea4335" : pct > 50 ? "#fbbc05" : "#34a853";

      // Back-pressure pill
      const pill = document.getElementById("bpPill");
      if (m.back_pressure) {
        pill.className = "metric-pill high";
        pill.textContent = "HIGH (" + (m.back_pressure_pct ?? 0) + "%)";
      } else {
        pill.className = "metric-pill ok";
        pill.textContent = "OK (" + (m.back_pressure_pct ?? 0) + "%)";
      }

      // Status dot + text
      const dot = document.getElementById("statusDot");
      const statusText = document.getElementById("statusText");
      if (m.crawl_running) {
        dot.className = "running";
        statusText.textContent = "Crawling…";
      } else {
        dot.className = "idle";
        statusText.textContent = "Idle";
      }

      // Recent Crawls
      const sessions = m.sessions || [];
      const recentEl = document.getElementById("recentCrawlers");
      if (sessions.length === 0) {
        recentEl.innerHTML = '<span class="recent-crawls-empty">No crawls started yet.</span>';
      } else {
        const frag = document.createDocumentFragment();
        // Show newest first
        [...sessions].reverse().forEach(s => {
          const row = document.createElement("div");
          row.className = "recent-crawl-item";

          const badgeClass = s.status === "Active" ? "active" : "finished";
          const badge = document.createElement("span");
          badge.className = "status-badge " + badgeClass;
          badge.textContent = s.status;

          const urlSpan = document.createElement("span");
          urlSpan.className = "recent-crawl-url";
          urlSpan.title = s.seed_url;
          urlSpan.textContent = s.seed_url;

          const timeSpan = document.createElement("span");
          timeSpan.className = "recent-crawl-time";
          timeSpan.textContent = s.time;

          row.appendChild(badge);
          row.appendChild(urlSpan);
          row.appendChild(timeSpan);
          frag.appendChild(row);
        });
        recentEl.innerHTML = "";
        recentEl.appendChild(frag);
      }
    } catch (e) {
      log("Metrics fetch failed: " + e.message, "error");
    }
  }

  setInterval(pollMetrics, 2000);
  pollMetrics();

  // ── Search ────────────────────────────────────────────────────────────────
  document.getElementById("searchForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    const query = (document.getElementById("searchInput").value || "").trim();
    if (!query) {
      document.getElementById("searchInfo").textContent = "Please enter a non-empty query.";
      return;
    }
    document.getElementById("searchInfo").textContent = "Searching…";
    document.getElementById("results").innerHTML = "";
    log('Searching: "' + query + '"');

    try {
      const encodedQuery = encodeURIComponent(query);
      const data = await apiFetch("GET", "/search?query=" + encodedQuery + "&sortBy=relevance");
      const results = data.results || [];

      if (results.length === 0) {
        document.getElementById("searchInfo").textContent = 'No results found for "' + escHtml(query) + '".';
        log('No results for "' + query + '"', "warn");
        return;
      }

      document.getElementById("searchInfo").textContent =
        "Showing " + results.length + " result" + (results.length !== 1 ? "s" : "") +
        ' for "' + escHtml(query) + '".';
      log("Found " + (data.total ?? results.length) + " result(s).");

      const frag = document.createDocumentFragment();
      results.forEach(item => {
        const li = document.createElement("li");
        li.className = "result-item";

        const link = document.createElement("a");
        link.href = item.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.className = "result-link";
        link.textContent = item.url;

        const displayUrl = document.createElement("div");
        displayUrl.className = "result-display-url";
        displayUrl.textContent = item.url;

        const meta = document.createElement("div");
        meta.className = "result-meta";
        const origin = item.origin_url || "seed";
        const score = item.relevance_score !== undefined ? item.relevance_score.toFixed(2) : "N/A";
        const frequency = item.frequency !== undefined ? item.frequency : "N/A";
        meta.textContent = "Origin: " + origin + " · Depth: " + item.depth + " · Freq: " + frequency + " · Score: " + score;

        li.appendChild(link);
        li.appendChild(displayUrl);
        li.appendChild(meta);
        frag.appendChild(li);
      });
      document.getElementById("results").appendChild(frag);
    } catch (err) {
      document.getElementById("searchInfo").textContent = "Search failed: " + err.message;
      log("Search failed: " + err.message, "error");
    }
  });

  // ── Start Crawl ───────────────────────────────────────────────────────────
  document.getElementById("indexForm").addEventListener("submit", async function (e) {
    e.preventDefault();
    const url = (document.getElementById("indexUrlInput").value || "").trim();
    const depthRaw = parseInt(document.getElementById("indexDepthInput").value, 10);
    const maxDepth = isNaN(depthRaw) ? 2 : Math.max(0, Math.min(10, depthRaw));
    const capacityRaw = parseInt(document.getElementById("indexCapacityInput").value, 10);
    const capacity = isNaN(capacityRaw) ? 500 : Math.max(1, Math.min(10000, capacityRaw));
    const statusEl = document.getElementById("indexStatus");

    if (!url) {
      statusEl.textContent = "Please enter a valid URL.";
      statusEl.style.color = "#c5221f";
      return;
    }

    statusEl.textContent = "Starting crawl…";
    statusEl.style.color = "#5f6368";
    log("Starting crawl: " + url + " (depth=" + maxDepth + ", capacity=" + capacity + ")");

    try {
      const data = await apiFetch("POST", "/api/start", { seed_url: url, max_depth: maxDepth, capacity: capacity });
      if (data.error) {
        statusEl.textContent = data.error;
        statusEl.style.color = "#c5221f";
        log("Start error: " + data.error, "error");
      } else {
        statusEl.textContent = "✓ " + (data.message || "Crawl started.") + " Workers: " + (data.workers ?? "—");
        statusEl.style.color = "#137333";
        log("✓ " + data.message + " Workers: " + data.workers);
        document.getElementById("indexUrlInput").value = "";
        setTimeout(() => {
          statusEl.textContent = "";
          statusEl.style.color = "#5f6368";
        }, 5000);
      }
    } catch (err) {
      statusEl.textContent = "Request failed: " + err.message;
      statusEl.style.color = "#c5221f";
      log("Start failed: " + err.message, "error");
    }
  });

  // ── Stop & Save ───────────────────────────────────────────────────────────
  async function stopServer() {
    if (!confirm("Stop the server and save crawler state to disk?")) return;
    log("Requesting graceful shutdown…", "warn");
    try {
      const data = await apiFetch("POST", "/api/stop", {});
      log((data.message || "Shutdown initiated.") + " You may close this tab.", "warn");
    } catch (_) {
      log("Shutdown request sent. Server is stopping.", "warn");
    }
  }
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _server_instance

    host = os.environ.get("CRAWLER_HOST", DEFAULT_HOST)
    port = int(os.environ.get("CRAWLER_PORT", DEFAULT_PORT))

    # Attempt to restore persisted state
    if os.path.exists(STATE_FILE):
        try:
            loaded = CrawlerState.load_state(STATE_FILE)
            # Re-use the loaded state's structures but keep the bounded queue
            _state.visited._visited = loaded.visited._visited
            _state.inverted_index._index = loaded.inverted_index._index
            _state.metadata_map._metadata = loaded.metadata_map._metadata
            logger.info("Restored state from %s (%d visited URLs)", STATE_FILE, len(_state.visited))
        except Exception as exc:
            logger.warning("Could not load state file %s: %s", STATE_FILE, exc)

    # Register SIGINT / SIGTERM handlers
    signal.signal(signal.SIGINT,  _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    _server_instance = ThreadingHTTPServer((host, port), CrawlerHandler)
    logger.info("Dashboard available at  http://localhost:%d/", port)
    logger.info("Press Ctrl-C to stop and save state.")

    try:
        _server_instance.serve_forever()
    except KeyboardInterrupt:
        _graceful_shutdown()


if __name__ == "__main__":
    main()