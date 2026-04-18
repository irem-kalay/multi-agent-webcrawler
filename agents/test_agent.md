# Agent 5: QA & Integration Agent (Gemini)

 **Role:** Code Reviewer and Integration Tester 

**Description:** I tested the generated codes with Gemini.  Gemini was checking the integrations of the codes whether they were implemented correctly or not. It gives feedback on what to change in which file and for which agent.   It also generates new prompts for those agents on how to handle specific technical challenges.

---

## 1. Prompt for the Architect Agent

 **Task:** Refactor the core data structures to support word frequency and specific file storage requirements.

**Instructions:**
*  **Update the Inverted Index:** Change the `ThreadSafeInvertedIndex` structure.  Instead of storing a simple set of URLs, it must store word frequencies as `Dict[str, Dict[str, int]]` (mapping a word to a dictionary of URLs and their occurrence counts). 
*  **Persistence Logic:** Create a method to save the index data to a plain text file located at `data/storage/p.data`. 
*  **Format Requirement:** Every line in `p.data` must follow this exact format: `word url origin depth frequency`. 

## 2. Prompt for the Indexer/Crawler Agent

 **Task:** Update the parsing logic to count word frequencies. 

**Instructions:**
*  **Count, Don't Just Deduplicate:** When parsing and tokenizing in `parser.py`, do not use a Set to deduplicate words.  We need the exact count of how many times each word appears on the page. 
*  **Use Collections:** Use `collections.Counter` (or a similar approach) to calculate the frequency of each tokenized word. 
*  **Integration:** Pass this frequency data to the updated `ThreadSafeInvertedIndex` so that the exact occurrence count is recorded. 

## 3. Prompt for the Search Engine Agent

 **Task:** Implement a hard-coded mathematical ranking formula. 

**Instructions:**
*  **Rewrite Ranking:** Completely rewrite the ranking and sorting logic inside the `search()` method.
*  **Scoring Formula:** Calculate a `relevance_score` for each matched URL using this exact formula: `score = (frequency * 10) + 1000 - (depth * 5)`.
*  **Output Format:** Return a list of 4-element tuples (or a corresponding NamedTuple) in this format: `(url, origin_url, depth, relevance_score)`.  Sort the final results by this score in descending order. 

## 4. Prompt for the UI/CLI Dashboard Agent

 **Task:** Align the API endpoints and web server with the strict assignment requirements. 

**Instructions:**
*  **Port Update:** Change the default server port from `8080` to `3600`. 
*  **API Route:** Update the search API endpoint to accept `GET` requests at `/search?query=<word>&sortBy=relevance`.  Extract the query parameters from the URL. 
*  **UI Display:** Update the frontend HTML dashboard to handle the new API response and ensure that the `relevance_score` is clearly displayed under each search result. 

## 5. Prompt for Internationalization (Turkish Character Support)

**Task:** Fix `UnicodeEncodeError` and search matching for Turkish characters.

**Instructions:**
* **URL Encoding:** Implement a `safe_encode_url` helper in `parser.py` using IDNA for domains and percent-encoding for paths to prevent crashes on Turkish URLs.
* **Normalization:** Update `normalize_term` in `data_structures.py` to explicitly handle Turkish 'İ' and 'I' characters before applying the lowercase transformation to ensure correct matching.

## 6. Prompt for Performance Optimization (Priority Queue)

**Task:** Optimize the crawl order based on depth.

**Instructions:**
* **Priority Logic:** Refactor `CrawlQueue` in `data_structures.py` to wrap `queue.PriorityQueue`.
* **Depth Prioritization:** Ensure that tasks are prioritized by depth (lower depth = higher priority) so that pages closer to the seed URL are always processed first.