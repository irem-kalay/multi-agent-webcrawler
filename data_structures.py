"""Thread-safe core data structures for a concurrent web crawler.

This module is intentionally limited to Python's standard library so it can be
used in restricted environments. It provides:

* ``ThreadSafeVisitedSet`` to prevent duplicate crawls
* ``CrawlQueue`` to apply queue-based back-pressure
* ``ThreadSafeInvertedIndex`` for concurrent search indexing
* ``ThreadSafeMetadataMap`` for URL discovery metadata
* ``CrawlerState`` to persist and restore the crawler's in-memory state
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Optional, Set, Tuple


STATE_FILE_VERSION = 1


@dataclass(frozen=True, slots=True)
class CrawlTask:
    """A single unit of crawl work queued for a worker thread.

    Attributes:
        url: The absolute URL to fetch.
        depth: The current depth of ``url`` in the crawl graph.
        origin_url: The page that discovered this URL. For seed URLs, callers
            may use the seed URL itself or ``None``.
        max_depth: The deepest level workers are allowed to enqueue from this
            crawl branch.
    """

    url: str
    depth: int
    origin_url: Optional[str]
    max_depth: int

    def __post_init__(self) -> None:
        """Validate task fields at construction time."""
        if not isinstance(self.url, str) or not self.url.strip():
            raise ValueError("CrawlTask.url must be a non-empty string.")
        if not isinstance(self.depth, int) or self.depth < 0:
            raise ValueError("CrawlTask.depth must be a non-negative integer.")
        if self.origin_url is not None and not isinstance(self.origin_url, str):
            raise ValueError("CrawlTask.origin_url must be a string or None.")
        if not isinstance(self.max_depth, int) or self.max_depth < 0:
            raise ValueError("CrawlTask.max_depth must be a non-negative integer.")

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serializable representation of the task."""
        return {
            "url": self.url,
            "depth": self.depth,
            "origin_url": self.origin_url,
            "max_depth": self.max_depth,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "CrawlTask":
        """Build a task from a dictionary produced by :meth:`to_dict`."""
        return cls(
            url=_require_string(payload, "url"),
            depth=_require_non_negative_int(payload, "depth"),
            origin_url=_optional_string(payload.get("origin_url")),
            max_depth=_require_non_negative_int(payload, "max_depth"),
        )


@dataclass(frozen=True, slots=True)
class DiscoveryMetadata:
    """Discovery information attached to a crawled URL."""

    origin_url: Optional[str]
    depth: int

    def __post_init__(self) -> None:
        """Validate metadata fields at construction time."""
        if self.origin_url is not None and not isinstance(self.origin_url, str):
            raise ValueError("DiscoveryMetadata.origin_url must be a string or None.")
        if not isinstance(self.depth, int) or self.depth < 0:
            raise ValueError("DiscoveryMetadata.depth must be a non-negative integer.")

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serializable representation of the metadata."""
        return {"origin_url": self.origin_url, "depth": self.depth}

    @classmethod
    def from_dict(cls, payload: Dict[str, object]) -> "DiscoveryMetadata":
        """Build metadata from a dictionary produced by :meth:`to_dict`."""
        return cls(
            origin_url=_optional_string(payload.get("origin_url")),
            depth=_require_non_negative_int(payload, "depth"),
        )


class ThreadSafeVisitedSet:
    """A lock-protected set of URLs that have already been seen."""

    def __init__(self, urls: Optional[Iterable[str]] = None) -> None:
        self._lock = threading.Lock()
        self._visited: Set[str] = set()
        if urls is not None:
            for url in urls:
                self._validate_url(url)
                self._visited.add(url)

    def mark_visited(self, url: str) -> bool:
        """Add ``url`` to the visited set.

        Args:
            url: URL to register as visited.

        Returns:
            ``True`` if the URL was newly inserted, or ``False`` if another
            thread had already inserted it.

        Notes:
            Call this before scheduling work for a newly discovered URL. The
            return value lets workers perform an atomic "check then add" step.
        """
        self._validate_url(url)
        with self._lock:
            if url in self._visited:
                return False
            self._visited.add(url)
            return True

    def contains(self, url: str) -> bool:
        """Return ``True`` if ``url`` is already marked as visited."""
        self._validate_url(url)
        with self._lock:
            return url in self._visited

    def snapshot(self) -> Set[str]:
        """Return a copy of all visited URLs.

        The returned set is detached from internal state and can be iterated
        without holding the crawler's lock.
        """
        with self._lock:
            return set(self._visited)

    def __len__(self) -> int:
        """Return the number of visited URLs."""
        with self._lock:
            return len(self._visited)

    @staticmethod
    def _validate_url(url: str) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL must be a non-empty string.")


class CrawlQueue:
    """A ``queue.PriorityQueue`` wrapper that stores :class:`CrawlTask` instances.

    Tasks are automatically prioritized by depth (lower depth = higher priority).
    Seed URLs (depth=0) jump to the front of the line ahead of deeper links.
    
    The queue's ``maxsize`` provides natural back-pressure. Callers may use
    ``block=False`` when they want to fail fast instead of waiting for capacity.
    """

    def __init__(
        self,
        maxsize: int = 0,
        tasks: Optional[Iterable[CrawlTask]] = None,
    ) -> None:
        if not isinstance(maxsize, int) or maxsize < 0:
            raise ValueError("maxsize must be a non-negative integer.")
        self._queue: "queue.PriorityQueue[tuple]" = queue.PriorityQueue(maxsize=maxsize)
        # Thread-safe counter for tie-breaking when multiple tasks have same depth
        self._count = itertools.count()
        if tasks is not None:
            for task in tasks:
                self.add_task(task, block=False)

    @property
    def maxsize(self) -> int:
        """Return the configured queue capacity.

        A value of ``0`` means the queue is unbounded, matching ``queue.Queue``.
        """
        return self._queue.maxsize

    def add_task(
        self,
        task: CrawlTask,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """Enqueue a crawl task with priority based on depth.

        Lower depths (seed URLs) are prioritized ahead of deeper links.

        Args:
            task: The :class:`CrawlTask` to enqueue.
            block: Whether to wait for free capacity.
            timeout: Maximum seconds to wait when ``block=True``. Leave as
                ``None`` to wait indefinitely.

        Raises:
            TypeError: If ``task`` is not a :class:`CrawlTask`.
            queue.Full: If the queue is full and the caller uses ``block=False``
                or the timeout expires.

        Notes:
            Worker code that wants explicit back-pressure handling should call
            ``add_task(task, block=False)`` and catch ``queue.Full``.
        """
        if not isinstance(task, CrawlTask):
            raise TypeError("CrawlQueue only accepts CrawlTask instances.")
        # Wrap as (priority, count, task) tuple
        # Priority is task.depth (lower values = higher priority)
        # Count is used for tie-breaking when depths are equal
        priority_item = (task.depth, next(self._count), task)
        self._queue.put(priority_item, block=block, timeout=timeout)

    def put(
        self,
        task: CrawlTask,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        """Alias for :meth:`add_task` with ``queue.Queue``-style naming."""
        self.add_task(task, block=block, timeout=timeout)

    def get_task(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> CrawlTask:
        """Remove and return the next highest-priority task from the queue.

        Lower-depth tasks (seed URLs) are returned before deeper links.

        Args:
            block: Whether to wait for a task to become available.
            timeout: Maximum seconds to wait when ``block=True``. Leave as
                ``None`` to wait indefinitely.

        Raises:
            queue.Empty: If the queue is empty and the caller uses
                ``block=False`` or the timeout expires.
        """
        priority, count, task = self._queue.get(block=block, timeout=timeout)
        return task

    def get(
        self,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> CrawlTask:
        """Alias for :meth:`get_task` with ``queue.Queue``-style naming."""
        return self.get_task(block=block, timeout=timeout)

    def task_done(self) -> None:
        """Mark one previously fetched task as finished.

        Every successful call to :meth:`get_task` or :meth:`get` must be paired
        with exactly one call to ``task_done()`` after processing completes.
        """
        self._queue.task_done()

    def join(self) -> None:
        """Block until all queued tasks have been marked done."""
        self._queue.join()

    def qsize(self) -> int:
        """Return the approximate number of pending tasks."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Return ``True`` when no tasks are currently pending."""
        return self._queue.empty()

    def full(self) -> bool:
        """Return ``True`` when no additional tasks can be enqueued."""
        return self._queue.full()

    def snapshot(self) -> List[CrawlTask]:
        """Return a point-in-time copy of tasks that are still pending.

        The snapshot includes tasks that remain in the queue, not tasks already
        handed to workers. Tasks are returned in priority order (seed URLs first).
        For a fully consistent shutdown snapshot, stop worker threads before
        saving state.
        """
        with self._queue.mutex:
            # PriorityQueue stores tuples (priority, count, task), extract tasks
            return [item[2] for item in list(self._queue.queue)]

    def set_maxsize(self, new_size: int) -> None:
        """Dynamically change the queue's maxsize (capacity).

        Thread-safe: acquires the internal queue mutex before updating.
        A size of ``0`` means the queue becomes unbounded.

        Args:
            new_size: The new maximum capacity (non-negative integer).

        Raises:
            ValueError: If ``new_size`` is negative or the current queue size
                exceeds the new size (this prevents data loss).
        """
        if not isinstance(new_size, int) or new_size < 0:
            raise ValueError("new_size must be a non-negative integer.")

        with self._queue.mutex:
            current_size = len(self._queue.queue)
            if new_size > 0 and current_size > new_size:
                raise ValueError(
                    f"Cannot shrink queue to {new_size}: {current_size} tasks "
                    f"are already pending. Please wait for tasks to complete."
                )
            self._queue.maxsize = new_size


class ThreadSafeInvertedIndex:
    """A lock-protected mapping of normalized words to URL frequency dictionaries.
    
    Instead of storing a simple Set of URLs for each word, this structure stores
    the frequency of the word on each URL as Dict[str, Dict[str, int]]
    (word -> {url: occurrence_count}).
    """

    def __init__(self, initial: Optional[Dict[str, Dict[str, int]]] = None) -> None:
        self._lock = threading.RLock()
        self._index: DefaultDict[str, Dict[str, int]] = defaultdict(dict)
        if initial is not None:
            for word, url_freq_dict in initial.items():
                normalized_word = self.normalize_term(word)
                if normalized_word:
                    for url, frequency in url_freq_dict.items():
                        ThreadSafeVisitedSet._validate_url(url)
                        if not isinstance(frequency, int) or frequency < 0:
                            raise ValueError(f"Frequency must be a non-negative integer, got {frequency}")
                        self._index[normalized_word][url] = frequency

    def add_to_index(self, url: str, word_frequencies: Dict[str, int]) -> int:
        """Index ``url`` with word frequency data.
        
        Updates the index with the provided word-frequency pairs for the given URL.
        If a word is already indexed for this URL, the frequency is updated to the
        new value provided.

        Args:
            url: The page URL that contains the supplied words.
            word_frequencies: A dictionary mapping words to their occurrence counts on
                the page. Tokens are normalized to lowercase and stripped of surrounding
                whitespace.

        Returns:
            The number of distinct normalized terms updated for ``url``.

        Notes:
            Empty tokens are ignored. Frequencies must be non-negative integers.
        """
        ThreadSafeVisitedSet._validate_url(url)
        normalized_words = {}
        for word, frequency in word_frequencies.items():
            if not isinstance(frequency, int) or frequency < 0:
                raise ValueError(f"Frequency must be a non-negative integer for word '{word}'")
            normalized = self.normalize_term(word)
            if normalized:  # Skip empty normalized tokens
                normalized_words[normalized] = frequency

        if not normalized_words:
            return 0

        with self._lock:
            for word, frequency in normalized_words.items():
                self._index[word][url] = frequency
        return len(normalized_words)

    def get_urls(self, word: str) -> Dict[str, int]:
        """Return a copy of the URL-to-frequency mapping for ``word``.

        The returned dict is detached from internal state so search threads can
        use it without holding the index lock.
        
        Returns:
            A dictionary mapping URLs to their frequency counts for the given word.
        """
        normalized_word = self.normalize_term(word)
        if not normalized_word:
            return {}
        with self._lock:
            return dict(self._index.get(normalized_word, {}))

    def get_all_urls_for_word(self, word: str) -> Set[str]:
        """Return a set of all URLs that contain the given word (for backward compatibility).

        This is a convenience method that extracts just the URLs from the frequency dict.
        Useful for AND query operations.
        """
        return set(self.get_urls(word).keys())

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return a deep copy of the complete inverted index."""
        with self._lock:
            return {word: dict(url_freq_dict) for word, url_freq_dict in self._index.items()}

    def __len__(self) -> int:
        """Return the number of indexed terms."""
        with self._lock:
            return len(self._index)

    def save_to_file(self, file_path: str, metadata_map: "ThreadSafeMetadataMap") -> Path:
        """Save the inverted index to a plain text file.
        
        Creates or appends to a file at the specified path. Each line follows the format:
        word url origin depth frequency
        
        Args:
            file_path: The path to save to (typically 'data/storage/p.data')
            metadata_map: The metadata map to look up origin_url and depth for each URL
        
        Returns:
            The resolved path to the saved file
        """
        destination = Path(file_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            with destination.open("w", encoding="utf-8") as handle:
                snapshot = {word: dict(url_freq_dict) for word, url_freq_dict in self._index.items()}
                
                for word in sorted(snapshot.keys()):
                    url_freq_dict = snapshot[word]
                    for url in sorted(url_freq_dict.keys()):
                        frequency = url_freq_dict[url]
                        # Get metadata for this URL
                        metadata = metadata_map.get(url)
                        origin = metadata.origin_url if metadata else None
                        depth = metadata.depth if metadata else 0
                        
                        # Format: word url origin depth frequency
                        line = f"{word} {url} {origin or 'None'} {depth} {frequency}\n"
                        handle.write(line)
        
        return destination

    @staticmethod
    def normalize_term(term: object) -> str:
        """Normalize a token for storage and lookup.

        Non-string values are ignored and normalized to ``""``.
        """
        if not isinstance(term, str):
            return ""
        return term.strip().lower()


class ThreadSafeMetadataMap:
    """A lock-protected mapping from URL to discovery metadata."""

    def __init__(self, initial: Optional[Dict[str, DiscoveryMetadata]] = None) -> None:
        self._lock = threading.RLock()
        self._metadata: Dict[str, DiscoveryMetadata] = {}
        if initial is not None:
            for url, metadata in initial.items():
                ThreadSafeVisitedSet._validate_url(url)
                if not isinstance(metadata, DiscoveryMetadata):
                    raise TypeError(
                        "ThreadSafeMetadataMap initial values must be DiscoveryMetadata."
                    )
                self._metadata[url] = metadata

    def record(self, url: str, origin_url: Optional[str], depth: int) -> DiscoveryMetadata:
        """Store discovery metadata for ``url``.

        Args:
            url: The discovered URL.
            origin_url: The page that contained ``url``. For seed URLs, callers
                may pass the seed itself or ``None``.
            depth: Crawl depth assigned to ``url``.

        Returns:
            The metadata currently stored for ``url``.

        Notes:
            If the URL already exists, the shallower depth wins. This makes the
            structure stable under races where the same URL is discovered from
            multiple pages at nearly the same time.
        """
        ThreadSafeVisitedSet._validate_url(url)
        metadata = DiscoveryMetadata(origin_url=origin_url, depth=depth)
        with self._lock:
            current = self._metadata.get(url)
            if current is None or metadata.depth < current.depth:
                self._metadata[url] = metadata
            return self._metadata[url]

    def get(self, url: str) -> Optional[DiscoveryMetadata]:
        """Return the stored metadata for ``url``, or ``None`` if absent."""
        ThreadSafeVisitedSet._validate_url(url)
        with self._lock:
            return self._metadata.get(url)

    def snapshot(self) -> Dict[str, DiscoveryMetadata]:
        """Return a copy of the full URL-to-metadata mapping."""
        with self._lock:
            return dict(self._metadata)

    def __len__(self) -> int:
        """Return the number of URLs with stored metadata."""
        with self._lock:
            return len(self._metadata)


@dataclass(slots=True)
class CrawlerState:
    """Dataclass container for the crawler's shared thread-safe structures."""

    visited: ThreadSafeVisitedSet = field(default_factory=ThreadSafeVisitedSet)
    crawl_queue: CrawlQueue = field(default_factory=CrawlQueue)
    inverted_index: ThreadSafeInvertedIndex = field(
        default_factory=ThreadSafeInvertedIndex
    )
    metadata_map: ThreadSafeMetadataMap = field(default_factory=ThreadSafeMetadataMap)
    _persistence_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def save_state(self, file_path: str = "state.json") -> Path:
        """Persist the current crawler state to JSON.

        Args:
            file_path: Destination JSON file. Existing files are replaced
                atomically using a temporary file and ``os.replace``.

        Returns:
            The resolved path to the saved state file.

        Notes:
            For the most consistent snapshot, stop or pause worker threads
            before calling this method so no tasks are in flight.
        """
        destination = Path(file_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)

        with self._persistence_lock:
            payload = {
                "version": STATE_FILE_VERSION,
                "visited_urls": sorted(self.visited.snapshot()),
                "crawl_queue": {
                    "maxsize": self.crawl_queue.maxsize,
                    "pending_tasks": [
                        task.to_dict() for task in self.crawl_queue.snapshot()
                    ],
                },
                "inverted_index": {
                    word: url_freq_dict
                    for word, url_freq_dict in sorted(self.inverted_index.snapshot().items())
                },
                "metadata_map": {
                    url: metadata.to_dict()
                    for url, metadata in sorted(self.metadata_map.snapshot().items())
                },
            }

            temp_path = destination.with_name(destination.name + ".tmp")
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, destination)

        return destination

    @classmethod
    def load_state(
        cls,
        file_path: str = "state.json",
        queue_maxsize: Optional[int] = None,
    ) -> "CrawlerState":
        """Load crawler state from ``file_path``.

        Args:
            file_path: Source JSON file created by :meth:`save_state`.
            queue_maxsize: Optional override for the restored queue capacity. If
                provided, it must be large enough to hold every pending task in
                the saved file.

        Returns:
            A new :class:`CrawlerState` instance populated from disk.

        Raises:
            FileNotFoundError: If ``file_path`` does not exist.
            ValueError: If the saved file version is unsupported or the queue
                override is smaller than the number of pending tasks.
        """
        source = Path(file_path).expanduser().resolve()
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)

        version = payload.get("version")
        if version != STATE_FILE_VERSION:
            raise ValueError(
                f"Unsupported state file version: {version!r}. "
                f"Expected {STATE_FILE_VERSION}."
            )

        visited_urls = payload.get("visited_urls", [])
        if not isinstance(visited_urls, list):
            raise ValueError("State file field 'visited_urls' must be a list.")

        crawl_queue_payload = payload.get("crawl_queue", {})
        if not isinstance(crawl_queue_payload, dict):
            raise ValueError("State file field 'crawl_queue' must be an object.")

        saved_maxsize = _require_non_negative_int(crawl_queue_payload, "maxsize")
        pending_task_payloads = crawl_queue_payload.get("pending_tasks", [])
        if not isinstance(pending_task_payloads, list):
            raise ValueError("State file field 'pending_tasks' must be a list.")

        pending_tasks = [CrawlTask.from_dict(item) for item in pending_task_payloads]
        effective_maxsize = saved_maxsize if queue_maxsize is None else queue_maxsize
        if effective_maxsize < 0:
            raise ValueError("queue_maxsize must be non-negative.")
        if effective_maxsize > 0 and len(pending_tasks) > effective_maxsize:
            raise ValueError(
                "queue_maxsize is smaller than the number of pending tasks in state."
            )

        inverted_index_payload = payload.get("inverted_index", {})
        if not isinstance(inverted_index_payload, dict):
            raise ValueError("State file field 'inverted_index' must be an object.")

        metadata_payload = payload.get("metadata_map", {})
        if not isinstance(metadata_payload, dict):
            raise ValueError("State file field 'metadata_map' must be an object.")

        visited = ThreadSafeVisitedSet(visited_urls)
        crawl_queue = CrawlQueue(maxsize=effective_maxsize, tasks=pending_tasks)
        inverted_index = ThreadSafeInvertedIndex(inverted_index_payload)
        metadata_map = ThreadSafeMetadataMap(
            {
                url: DiscoveryMetadata.from_dict(raw_metadata)
                for url, raw_metadata in metadata_payload.items()
            }
        )

        return cls(
            visited=visited,
            crawl_queue=crawl_queue,
            inverted_index=inverted_index,
            metadata_map=metadata_map,
        )


def _require_string(payload: Dict[str, object], key: str) -> str:
    """Return a required string field from a decoded JSON object."""
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"State field {key!r} must be a non-empty string.")
    return value


def _optional_string(value: object) -> Optional[str]:
    """Validate an optional string value from a decoded JSON object."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional string field must be a string or None.")
    return value


def _require_non_negative_int(payload: Dict[str, object], key: str) -> int:
    """Return a required non-negative integer field from a decoded JSON object."""
    value = payload.get(key)
    if not isinstance(value, int) or value < 0:
        raise ValueError(f"State field {key!r} must be a non-negative integer.")
    return value
