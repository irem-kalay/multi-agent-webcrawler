# Agent 1: Architect Agent (Codex)

  **Role:** Expert System Architect.
  
  **Task:** Design the core thread-safe data structures for a highly concurrent Web Crawler and Search Engine.

---

## Strict Constraints
* **Native Focus:** You MUST ONLY use Python's standard library (e.g., `threading`, `queue`, `json`).   No third-party libraries are allowed.

---

## Requirements

### 1. ThreadSafeVisitedSet
  A set designed to ensure no URL is crawled more than once, protected by a thread-safe lock.

### 2. CrawlQueue
  A wrapper around `queue.Queue` (or `queue.PriorityQueue`) with a configurable `maxsize` to enforce back-pressure.
*   It must store `CrawlTask` objects containing the URL, depth, origin URL, and max depth.

### 3. ThreadSafeInvertedIndex
  A thread-safe dictionary mapping normalized words to a collection of URLs and their respective occurrence frequencies.

### 4. ThreadSafeMetadataMap
  A thread-safe dictionary mapping a URL to its discovery metadata, specifically the `origin_url` and the `depth` at which it was found.

### 5. State Persistence
  Implement methods to save these structures to a `state.json` file and load them to allow the crawler to resume after interruptions.

### 6. API Definition
  Write clear docstrings for all public methods (e.g., `add_task`, `add_to_index`) to ensure the Crawler and Search agents can interact with these structures correctly.

---

## Output
*   Provide the complete, production-ready Python code for `data_structures.py`.