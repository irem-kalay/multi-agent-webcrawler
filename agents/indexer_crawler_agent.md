# Agent 2: Crawler Engineer (GitHub Copilot)

**Role:** Backend Crawler Engineer  
**Task:** Build concurrent HTML parsing and URL fetching worker threads

---

## Context
You must utilize the following thread-safe structures from `data_structures.py`:
* `CrawlQueue`
* `ThreadSafeVisitedSet`
* `ThreadSafeInvertedIndex`
* `ThreadSafeMetadataMap`

---

## Strict Constraints
* **Library Restriction:** You MUST ONLY use `urllib` and `html.parser`.
* **Prohibited Libraries:** The use of `requests` and `BeautifulSoup` is STRICTLY PROHIBITED.

---

## Requirements

### 1. NativeHTMLParser
Create a class inheriting from `html.parser.HTMLParser` to:
* Extract visible text from pages.
* Ignore all content inside `<script>` and `<style>` tags.
* Extract all `href` links from anchor tags.

### 2. Worker Thread Logic
Implement a worker that continuously pulls crawl tasks from the `CrawlQueue`.



### 3. Depth Rule
Ensure that newly discovered links are only crawled if the `current_depth < max_depth`.

### 4. Metadata Recording
For every valid link discovered during the crawl:
* Record its `origin_url` (the source page).
* Record the `depth` at which it was found.
* Store this metadata in the `ThreadSafeMetadataMap`.

### 5. Back-Pressure Handling (CRITICAL)
When adding new links to the queue, implement the following safety mechanism:
* Use `queue.put(item, block=False)`.
* Catch any `queue.Full` exceptions.
* Log a back-pressure warning and immediately break out of the link-adding loop for that specific page to prevent deadlocks.

### 6. Indexing
Tokenize the extracted visible text and add the resulting words to the `ThreadSafeInvertedIndex`.

---

## Output
* Provide the complete, production-ready Python code for `parser.py`.