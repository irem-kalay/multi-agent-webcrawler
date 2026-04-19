# Multi-Agent Workflow Documentation

## 1. Orchestration Strategy
This project was developed using a **Manual Orchestration** approach for a Multi-Agent AI workflow. Instead of using a black-box autonomous framework (like AutoGPT), I acted as the **Product Manager and Tech Lead**. I defined specific roles for different LLMs, chained their inputs and outputs sequentially, and established a strict feedback/QA loop to ensure architectural consistency and adherence to assignment requirements.

## 2. Agent Roster & Responsibilities
The workflow consisted of 5 distinct agents, leveraging different LLM tools to utilize their specific strengths. Their detailed prompt files can be found in the `/agents` directory.

* **Agent 1: Architect Agent (Codex)**
    * *Role:* System Architect.
    * *Responsibility:* Design and generate **`data_structures.py`**. Responsible for creating the core thread-safe data structures (`ThreadSafeVisitedSet`, `CrawlQueue`, `ThreadSafeInvertedIndex`) using ONLY native Python libraries.
* **Agent 2: Crawler Engineer (GitHub Copilot)**
    * *Role:* Backend Crawler Engineer.
    * *Responsibility:* Design and generate **`parser.py`**. Responsible for building the worker threads to fetch, parse HTML, and handle back-pressure without deadlocking.
* **Agent 3: Search Engine Engineer (Cursor)**
    * *Role:* Search Algorithm Specialist.
    * *Responsibility:* Design and generate **`search_engine.py`**. Responsible for implementing concurrent read operations for the inverted index and calculating relevance scores based on the exact assignment formula.
* **Agent 4: Full-Stack Developer (ChatGPT)**
    * *Role:* UI/CLI Dashboard Developer.
    * *Responsibility:* Design and generate **`webserver.py`**. Responsible for building the native `http.server` to expose API endpoints and serve the frontend interactive dashboard.
* **Agent 5: QA & Integration Agent (Gemini)**
    * *Role:* Code Reviewer and Integration Tester.
    * *Responsibility:* Evaluate the combined outputs of Agents 1-4. Responsible for detecting race conditions, verifying strict assignment constraints, and providing feedback prompts to update the other agents' specific files.

## 3. Workflow, Interactions, and Decisions

The development process followed a "Design -> Generate -> Review -> Refine" loop.

**Phase 1: Sequential Handoff (Design & Generate)**
1.  I initiated the project by prompting the **Architect Agent** to build `data_structures.py`. I explicitly constrained it to use native libraries.
2.  I took the resulting API definitions and passed them as context to the **Crawler Agent** and **Search Engine Agent**, assigning them to build their respective modules around the Architect's structures.
3.  I then provided the generated backend function signatures to the **Full-Stack Developer (UI Agent)**, instructing it to build `webserver.py`. This agent was tasked with wrapping the underlying engine logic into native HTTP endpoints (`/api/start`, `/api/search`) and serving the frontend dashboard.
4.  *Tech Lead Decision:* To satisfy the requirement that "search can be invoked while the indexer is active," I instructed the Architect to implement a `snapshot()` and detached dictionary copies mechanism. This ensured the Search Agent could read without acquiring a persistent lock on the Crawler's write operations.

**Phase 2: The QA Feedback Loop (Interaction & Refinement)**
Once the initial codebase was drafted, I fed the entire system to the **QA Agent (Gemini)** for integration testing against the strict assignment rubric. This triggered critical cross-agent interactions:

* *Interaction A (The Frequency Bug):* The QA Agent detected that the Crawler was using a `Set` to deduplicate words on a page, losing word frequency data. 
* *Tech Lead Decision:* I passed this feedback back to the **Crawler Agent** to use `collections.Counter`, and to the **Architect Agent** to update the inverted index schema to `Dict[str, Dict[str, int]]`.

* *Interaction B (Hard-coded Constraints):* The QA Agent noticed the project lacked the specific formula `score = (frequency * 10) + 1000 - (depth * 5)` and the `p.data` storage format. 
* *Tech Lead Decision:* I generated new strict prompts for the **Search Engine Agent** to implement the exact scoring formula, and for the **UI Agent** to extract the new `frequency` variable and display it on the dashboard. I also forced the Architect to implement a specific `save_to_file` method for `p.data`.

* *Interaction C (The Priority Queue Optimization):* During active crawling, I observed that the system was using a standard FIFO `queue.Queue`. This meant newly discovered, shallower links were not being prioritized over deeper links already waiting in the queue. I consulted the **QA Agent (Gemini)**, asking if implementing a `PriorityQueue` based on crawl depth would be more logical.
* *Tech Lead Decision:* The QA Agent validated this architectural improvement and generated a specific revision prompt. I passed this prompt to the **Architect Agent**, instructing it to refactor the `CrawlQueue` class to wrap `queue.PriorityQueue`. This ensured that tasks with a lower depth (closer to the seed URL) are always executed first, significantly optimizing the crawl order.

* *Interaction D (UI/UX Alignment and API Wiring):* I realized that the initially generated dashboard by the UI Agent was a generic, dark-themed developer console, which lacked user-friendliness. I envisioned a clean, minimalist "Google-in-a-Day" light theme instead.
* *Tech Lead Decision:* I rejected the initial design and prompted the **UI Agent (ChatGPT)** to completely rewrite the embedded HTML/CSS inside `webserver.py` to match a minimalist light theme. I explicitly forced the agent to properly wire the new JavaScript fetch calls to our newly established backend APIs (e.g., `POST /api/search` with JSON bodies) to ensure a seamless UI without breaking the backend logic.

* *Interaction E (Internationalization & Turkish Character Bug):* During deep crawling, the system crashed with a `UnicodeEncodeError` when `urllib` attempted to fetch URLs containing non-ASCII Turkish characters (e.g., "ı", "ü"). Furthermore, search queries containing Turkish uppercase letters (like "İ" or "I") were failing to match indexed words because Python's default `.lower()` method mishandles them.
* *Tech Lead Decision:* I consulted the **QA Agent (Gemini)** to diagnose the encoding issues and devised a two-part solution. First, I prompted the **Crawler Agent (GitHub Copilot)** to implement a `safe_encode_url` helper in `parser.py`, utilizing IDNA encoding for domains and percent-encoding for paths. Second, I prompted the **Architect Agent (Codex)** to update the `normalize_term` method in `data_structures.py` to explicitly map 'İ' to 'i' and 'I' to 'ı' before applying the lowercase transformation. This cross-agent coordination successfully resolved the internationalization bugs.

* *Interaction F (UI/UX Enhancement - Page Titles):* I noticed that displaying raw URLs in the search results did not provide a good "Google-in-a-Day" user experience. 
* *Tech Lead Decision:* I coordinated a full-stack feature update across all agents. I instructed the Crawler to extract `<title>` tags, the Architect to persist them in the metadata map, the Search Engine to return them in tuples, and the UI Agent to render them as clickable headers. This transformed the raw data output into a professional search engine interface.

## 4. Conclusion
By isolating responsibilities and utilizing a dedicated QA Agent, the multi-agent workflow successfully produced a highly concurrent, thread-safe application without relying on external libraries. The feedback loop proved essential in bridging the gap between "standard LLM output" and "strict assignment requirements," resulting in a robust system built entirely from scratch.
