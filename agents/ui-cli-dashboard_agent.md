# Agent 4: Full-Stack Dashboard Developer (ChatGPT)

**Role:** Full-Stack Dashboard Developer

**Task:** Build a web-based UI and an HTTP server to manage the crawler and display search results.

---

## Strict Constraints
* **Native Focus:** You MUST ONLY use Python's built-in `http.server`. 
* **Prohibited Frameworks:** The use of Flask, FastAPI, or any other external frameworks is STRICTLY PROHIBITED.

---

## Requirements

### 1. HTTP Server
Implement a custom `BaseHTTPRequestHandler` to:
* Serve a single-page HTML dashboard (embedded CSS/JS).
* Handle specific API routes for the crawler and search functions.

### 2. Start Indexing Endpoint
Provide a form or an API endpoint where:
* The user can input a **Seed URL** and **k (max_depth)**.
* Upon submission, the server initiates a crawl by pushing a new task to the `CrawlQueue`.

### 3. Real-Time Metrics Endpoint
Create a JSON endpoint that returns the current system state, including:
* Total number of visited URLs.
* Current depth of the `CrawlQueue`.
* **Back-Pressure Status:** A boolean or string indicator (e.g., alert if the queue is more than 80% full).

### 4. Search Interface
Provide a search bar on the dashboard. When a query is submitted:
* The server calls the search function provided by Agent 3.
* Results are displayed cleanly, showing the **relevant_url**, the **origin_url**, and the **depth** for each match.

### 5. Graceful Shutdown
Implement a signal handler (for `Ctrl+C`) that:
* Stops all active worker threads.
* Calls the architect's `save_state()` function to ensure data persistence before the process exits.

---

## Output
* Provide the complete Python code for `web_server.py`, including the embedded HTML, CSS, and JavaScript required for the interactive dashboard.