# Multi-Agent Web Crawler & Search Engine
github repository: https://github.com/irem-kalay/multi-agent-webcrawler
This project is a concurrent web crawler and search engine built entirely with the Python Standard Library, designed via a Multi-Agent AI workflow.

## How It Works
* **The Indexer (Crawler):** Worker threads continuously pull tasks from a thread-safe priority queue. They fetch web pages, extract text and links, and store word frequencies in an inverted index. A visited set ensures no duplicate crawling, while the bounded queue handles back-pressure.
* **The Search Engine:** When a user submits a query, the engine tokenizes it and performs an AND-intersection across the inverted index. It safely reads from the live index using detached snapshots, ensuring the crawler is never blocked. Results are ranked using a precise heuristic formula.
* **The Multi-Agent Workflow:** The codebase was generated, reviewed, and refined by a team of specialized AI agents (Architect, Crawler Engineer, Search Specialist, UI Developer) orchestrated by a human Tech Lead focusing on strict constraint adherence.

## Features
* **Thread-Safe Architecture:** Crawler workers and search queries run concurrently without blocking each other.
* **Back-Pressure Management:** Bounded queues prevent memory exhaustion during deep crawls.
* **Exact Formula Ranking:** Calculates relevance based on `(frequency * 10) + 1000 - (depth * 5)`.
* **Real-time Dashboard:** A native HTTP server provides a UI to monitor queue depths and execute searches.

## Setup & Execution

1.  **Requirements:** Python 3.8 or higher is required. No external packages (like `pip install ...`) are needed.
2.  **Run the Server:**
    Open your terminal, navigate to the project directory, and execute:
    ```bash
    python3 webserver.py
    ```
3.  **Access the Dashboard:**
    Open your web browser and go to:
    `http://localhost:3600/`

## How to Use
1.  **Start Indexing:** On the right side of the dashboard, enter a Seed URL (e.g., `https://www.itu.edu.tr/`), set a max depth, and click "Start Crawl".
2.  **Monitor:** Watch the real-time metrics update as worker threads fetch and index pages.
3.  **Search:** Use the search bar on the left to query terms (e.g., "python web"). The results will display the URL, origin, depth, frequency, and exact relevance score.
4.  **Graceful Shutdown:** Click "Stop & Save" on the top right (or press `Ctrl+C` in the terminal) to save the current crawl state to `crawler_state.json` and the index to `data/storage/p.data`.

## Resuming a Crawl
If you stop the server gracefully, restarting it via `python3 webserver.py` will automatically load the `crawler_state.json` file and resume from where it left off.
