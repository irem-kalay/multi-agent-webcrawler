# Agent 3: Search Engine Engineer (Cursor)

**Role:** Search Engine Engineer
**Task:** Implement the search logic that queries the Inverted Index while the crawler is actively running.

---

## Context
* Utilize the thread-safe data structures defined in `data_structures.py`.

---

## Requirements

### 1. Concurrency
The search function must be designed to safely perform read operations from the `ThreadSafeInvertedIndex` and `ThreadSafeMetadataMap`. It must ensure that background crawler threads are never blocked by search queries.

### 2. Query Processing
* Support multi-word string queries.
* Normalize and split the query into individual tokens.
* Implement **AND logic**, requiring that a result URL must contain every word present in the query.

### 3. Output Format (CRITICAL)
The search function MUST return a list of tuples (triples) exactly in the following format:
`[(relevant_url, origin_url, depth), ...]`
* The `origin_url` and `depth` must be retrieved from the `ThreadSafeMetadataMap` for each matching URL.

### 4. Ranking
Implement a ranking mechanism to order the results. This can be based on:
* Term frequency (the number of occurrences of the query words).
* Crawl depth (where a lower depth indicates a more relevant/authoritative result).

### 5. Implementation
Provide the core search logic, specifically a `search(query)` method or class that encapsulates the requirements above.

---

## Output
* Provide the complete Python code for `search_engine.py`.