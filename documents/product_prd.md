# Product Requirements Document (PRD)
**Product Name:** Concurrent Web Crawler & Search Engine

## 1. Objective
To build a highly concurrent, thread-safe web crawler and real-time search engine system from scratch using only Python's native language capabilities (no third-party frameworks like Scrapy, Requests, Flask, or Elasticsearch). The system must allow users to query the indexed data live, simultaneously while the background crawler is actively indexing new pages.

## 2. Core Features & Requirements

### 2.1. Indexer (Crawler)
* **Recursive Crawling:** Accepts a seed `origin` URL and initiates crawling to a maximum depth `k`.
* **Uniqueness:** Must implement a strict "Visited" set to ensure no page is ever crawled twice.
* **Concurrency & Safety:** Utilizes multi-threading to fetch and parse pages simultaneously. Must utilize custom thread-safe data structures (e.g., Mutexes, Concurrent Maps) to prevent data corruption during parallel operations.
* **Back-Pressure Management:** The system must manage its own load. It must implement a bounded queue (max rate of work/queue depth) and gracefully reject new links to prevent deadlocks and memory overflow.
* **Data Storage:** Word frequencies, origin URLs, and crawl depths must be indexed. Data must be persistently stored in a flat file format at `data/storage/p.data`.

### 2.2. Search Engine
* **Query Engine:** Accepts a multi-word string query and processes it using AND semantics.
* **Live Indexing (Concurrent Reads):** Search operations must run seamlessly while the indexer is active, without acquiring blocking locks that would halt crawler workers.
* **Relevancy (Ranking):** Results must be ranked based on a specific heuristic formula: `score = (frequency * 10) + 1000 - (depth * 5)`.
* **Output:** Returns a list of structured results containing: `(title, relevant_url, origin_url, depth, relevance_score, frequency)`.

### 2.3. System Visibility & Dashboard (UI/CLI)
* Provides a web-based dashboard running on `http://localhost:3600`.
* Allows users to dynamically initiate a crawl by inputting a seed URL and max depth.
* **Real-time Metrics:** Must display the state of the system in real-time, tracking:
  * Current Indexing Progress (URLs processed vs. queued).
  * Current Queue Depth.
  * Back-pressure / Throttling status.
* Provides a search interface that queries the live index and displays ranked results.

## 3. Non-Functional & Architectural Requirements
* **Native Focus:** Strictly limited to the Python Standard Library (`threading`, `queue`, `urllib`, `html.parser`, `http.server`).
* **Persistence:** Must support state saving (graceful shutdown via Ctrl+C) allowing the system to be resumed after an interruption without restarting the crawl from scratch.
* **Development Methodology:** Designed and built leveraging an AI-Augmented "Multi-Agent" workflow, overseen by a Human-in-the-Loop acting as the System Architect.
