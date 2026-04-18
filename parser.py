"""Concurrent HTML parsing and URL fetching worker threads for the web crawler.

This module provides:
* ``NativeHTMLParser`` - Extracts visible text and href links using html.parser
* ``fetch_and_parse()`` - Fetches a URL and extracts content using urllib
* ``worker_thread()`` - Worker that pulls tasks from queue and crawls pages
* ``start_workers()`` - Helper to launch multiple worker threads

Uses only urllib and html.parser (no requests or BeautifulSoup).
Implements back-pressure handling to avoid deadlocks when queue is full.
"""

import logging
import queue
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from typing import Dict, List, Set, Tuple

from data_structures import (
    CrawlQueue,
    CrawlTask,
    ThreadSafeVisitedSet,
    ThreadSafeInvertedIndex,
    ThreadSafeMetadataMap,
)

logger = logging.getLogger(__name__)


class NativeHTMLParser(HTMLParser):
    """Extract visible text and href links from HTML using only html.parser.
    
    Ignores text inside script and style tags to get clean, visible content.
    Extracts all href attributes from anchor tags.
    """

    def __init__(self) -> None:
        """Initialize the parser."""
        super().__init__()
        self.text_parts: List[str] = []
        self.links: Set[str] = set()
        self.in_script_or_style = False

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str]]) -> None:
        """Handle opening tags.
        
        Tracks when we enter script/style tags (to ignore their content).
        Extracts href attributes from anchor tags.
        """
        if tag in ("script", "style"):
            self.in_script_or_style = True
        elif tag == "a":
            for attr, value in attrs:
                if attr == "href" and value:
                    self.links.add(value)

    def handle_endtag(self, tag: str) -> None:
        """Handle closing tags.
        
        Marks when we exit script/style tags.
        """
        if tag in ("script", "style"):
            self.in_script_or_style = False

    def handle_data(self, data: str) -> None:
        """Extract visible text (unless inside script/style tags)."""
        if not self.in_script_or_style:
            text = data.strip()
            if text:
                self.text_parts.append(text)

    def get_text(self) -> str:
        """Return concatenated visible text."""
        return " ".join(self.text_parts)

    def get_links(self) -> Set[str]:
        """Return all extracted links."""
        return self.links


def fetch_and_parse(url: str, timeout: int = 10) -> Tuple[str, Set[str]]:
    """Fetch a URL and parse its HTML using urllib.
    
    Sends requests with a standard browser User-Agent header to avoid
    HTTP 403 Forbidden errors from sites that block non-browser requests.
    
    Args:
        url: The URL to fetch (must be valid HTTP/HTTPS).
        timeout: Request timeout in seconds. Defaults to 10.
    
    Returns:
        A tuple of (visible_text, links) extracted from the page.
    
    Raises:
        urllib.error.URLError: If the URL is invalid or unreachable.
        Exception: If parsing fails.
    """
    try:
        # Create a Request object with a standard browser User-Agent header
        # to avoid HTTP 403 Forbidden errors from sites blocking non-browser requests
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode("utf-8", errors="ignore")

        parser = NativeHTMLParser()
        parser.feed(content)

        return parser.get_text(), parser.get_links()
    except Exception as e:
        logger.error(f"Failed to fetch or parse {url}: {e}")
        raise


def normalize_url(url: str, base_url: str) -> str:
    """Normalize a relative URL to an absolute URL.
    
    Handles relative URLs by joining them with the base_url using urljoin.
    
    Args:
        url: The URL to normalize (may be relative like '/path' or '../page').
        base_url: The page where the URL was found (base for resolution).
    
    Returns:
        An absolute URL.
    """
    return urllib.parse.urljoin(base_url, url)


def tokenize_text(text: str) -> List[str]:
    """Tokenize text into words for indexing with Unicode/Turkish support.
    
    Uses regex with Unicode flag to extract word boundaries, properly handling
    Unicode and Turkish characters. Converts to lowercase with special handling
    for Turkish characters (İ -> i, I -> ı).
    
    Args:
        text: The text to tokenize.
    
    Returns:
        A list of normalized word tokens.
    """
    # Handle Turkish uppercase characters before lowercasing
    # İ (Turkish uppercase dotted I) -> i
    # I (ASCII uppercase I) -> ı (Turkish lowercase undotted i)
    text = text.replace("İ", "i").replace("I", "ı").lower()
    
    # Extract words using regex with Unicode support
    # \w+ matches word characters including Unicode letters
    tokens = re.findall(r"\w+", text, flags=re.UNICODE)
    return [token for token in tokens if token]


def calculate_word_frequencies(text: str) -> Dict[str, int]:
    """Calculate word frequencies from text using collections.Counter.
    
    Tokenizes the text and returns exact counts of how many times each word
    appears, without deduplication. Handles Unicode and Turkish characters.
    
    Args:
        text: The text to analyze.
    
    Returns:
        A dictionary mapping each word to its occurrence count.
    """
    tokens = tokenize_text(text)
    return dict(Counter(tokens))


def worker_thread(
    worker_id: int,
    crawl_queue: CrawlQueue,
    visited_set: ThreadSafeVisitedSet,
    inverted_index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    timeout: int = 10,
) -> None:
    """Worker thread that continuously processes crawl tasks.
    
    Each worker:
    1. Pulls a CrawlTask from the shared queue
    2. Fetches and parses the URL using urllib and NativeHTMLParser
    3. Indexes the visible text into the inverted index
    4. Extracts links and filters by depth constraint (current_depth < max_depth)
    5. For each unique link:
       - Marks it as visited (atomic operation)
       - Records its metadata (origin_url and depth)
       - Tries to enqueue a task with back-pressure handling
       - If queue is full (back-pressure), logs warning and breaks link loop
    6. Marks the task as done so queue can track progress
    
    Implements back-pressure handling to avoid deadlocks: when the queue is 
    full, the worker stops trying to add more links from the current page
    but still completes processing of that page.
    
    Args:
        worker_id: Identifier for this worker thread (for logging).
        crawl_queue: Shared CrawlQueue that threads pull tasks from.
        visited_set: Shared ThreadSafeVisitedSet to prevent duplicate crawls.
        inverted_index: Shared ThreadSafeInvertedIndex for text search indexing.
        metadata_map: Shared ThreadSafeMetadataMap tracking URL discovery info.
        timeout: Request timeout in seconds. Defaults to 10.
    """
    logger.info(f"Worker {worker_id} started")

    while True:
        try:
            # Use a short timeout so we can be interrupted if needed
            task = crawl_queue.get_task(block=True, timeout=1)
        except queue.Empty:
            # No task available, loop back and try again
            continue

        try:
            logger.info(f"Worker {worker_id} processing: {task.url}")

            # Fetch and parse the page
            text, links = fetch_and_parse(task.url, timeout=timeout)

            # Calculate word frequencies and index the visible text
            word_frequencies = calculate_word_frequencies(text)
            if word_frequencies:
                inverted_index.add_to_index(task.url, word_frequencies)
                logger.debug(
                    f"Worker {worker_id} indexed {len(word_frequencies)} unique terms "
                    f"({sum(word_frequencies.values())} total occurrences) from {task.url}"
                )

            # Process discovered links if we haven't reached max depth
            if task.depth < task.max_depth:
                for link in links:
                    try:
                        # Normalize relative URLs to absolute URLs
                        absolute_url = normalize_url(link, task.url)

                        # Mark URL as visited (atomic check-then-add)
                        # Returns True if newly visited, False if already visited
                        if visited_set.mark_visited(absolute_url):
                            # Record discovery metadata for this newly found URL
                            metadata_map.record(
                                url=absolute_url,
                                origin_url=task.url,
                                depth=task.depth + 1,
                            )

                            # Create a task for the newly discovered URL
                            new_task = CrawlTask(
                                url=absolute_url,
                                depth=task.depth + 1,
                                origin_url=task.url,
                                max_depth=task.max_depth,
                            )

                            # Try to enqueue with back-pressure handling
                            try:
                                crawl_queue.add_task(new_task, block=False)
                                logger.debug(
                                    f"Worker {worker_id} queued: {absolute_url}"
                                )
                            except queue.Full:
                                # CRITICAL: Back-pressure detected
                                # Log warning, break link loop to avoid deadlock
                                logger.warning(
                                    f"Worker {worker_id}: Queue full ({crawl_queue.qsize()}/{crawl_queue.maxsize}), "
                                    f"back-pressure on {task.url}. Stopping link processing for this page."
                                )
                                break
                    except Exception as e:
                        logger.error(
                            f"Worker {worker_id}: Error processing link {link} from {task.url}: {e}"
                        )

            # Mark task as complete so queue can track overall progress
            crawl_queue.task_done()
            logger.debug(f"Worker {worker_id} completed: {task.url}")

        except Exception as e:
            # Catch any unhandled errors in page processing
            logger.error(f"Worker {worker_id} error processing {task.url}: {e}")
            # Still mark task done to prevent queue.join() from hanging
            crawl_queue.task_done()


def start_workers(
    num_workers: int,
    crawl_queue: CrawlQueue,
    visited_set: ThreadSafeVisitedSet,
    inverted_index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    timeout: int = 10,
) -> List[threading.Thread]:
    """Start worker threads that process the crawl queue.
    
    Creates and starts the specified number of daemon worker threads. Each
    thread calls worker_thread() and continuously processes tasks until the
    main program exits.
    
    Args:
        num_workers: Number of worker threads to start (typically 2-8).
        crawl_queue: Shared CrawlQueue to pull tasks from.
        visited_set: Shared ThreadSafeVisitedSet tracking visited URLs.
        inverted_index: Shared ThreadSafeInvertedIndex for indexing.
        metadata_map: Shared ThreadSafeMetadataMap for URL metadata.
        timeout: Request timeout in seconds. Defaults to 10.
    
    Returns:
        A list of started Thread objects (all daemon threads).
        Threads will exit when the main program ends.
    """
    workers = []
    for i in range(num_workers):
        thread = threading.Thread(
            target=worker_thread,
            args=(
                i,
                crawl_queue,
                visited_set,
                inverted_index,
                metadata_map,
                timeout,
            ),
            daemon=True,
            name=f"CrawlWorker-{i}",
        )
        thread.start()
        workers.append(thread)
        logger.info(f"Started worker thread {i}")
    return workers
