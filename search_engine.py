"""Concurrent-safe search engine over a live inverted index.

This module provides:
* ``SearchResult``   - Typed named tuple for a single search result.
* ``SearchEngine``   - Class encapsulating query logic over shared data structures.
* ``search()``       - Module-level convenience wrapper around ``SearchEngine``.

Design principles
-----------------
Concurrency
    Every read is performed through the fine-grained locking already built into
    ``ThreadSafeInvertedIndex`` and ``ThreadSafeMetadataMap``.  Specifically:

    * ``inverted_index.get_urls(token)`` acquires the index lock only for the
      duration of the copy, then returns a *detached* ``set[str]``.  The
      search thread can intersect those sets freely, without holding any lock.
    * ``metadata_map.get(url)`` acquires the metadata lock for a single
      dict-lookup and immediately releases it.

    There is no cross-structure lock, so crawler workers are never blocked by
    a running query and queries are never blocked by each other.

Query processing
    The raw query string is normalised and split into tokens using the same
    ``normalize_term`` helper exposed by ``ThreadSafeInvertedIndex``, so the
    search vocabulary is always consistent with the stored index.

AND semantics
    A URL must appear in *every* token's posting list to be included in the
    results.  The implementation starts from the smallest posting list and
    intersects outward, which minimises the total work done.

Ranking  (two-tier)
    1. *Depth* (ascending) — pages discovered closer to the seed URL are
       considered more authoritative / canonical.
    2. *Term-frequency proxy* (descending) — for URLs at equal depth, the
       engine counts how many unique index terms are attributed to that URL.
       Because ``add_to_index`` deduplicates tokens within a single call, this
       count reflects the vocabulary breadth of the page, a lightweight proxy
       for richer content.  The full-index snapshot is taken once and cached
       inside the query call; it is *not* held as a long-lived lock.

Output
    A list of ``(url, origin_url, depth)`` triples, sorted best-first, with
    length bounded by ``max_results``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

from data_structures import (
    ThreadSafeInvertedIndex,
    ThreadSafeMetadataMap,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

class SearchResult(NamedTuple):
    """A single ranked search result.

    Attributes:
        url:               The matching page URL.
        origin_url:        The page that first discovered ``url`` (may be ``None``
                           for seed URLs).
        depth:             Crawl depth at which ``url`` was discovered.
        relevance_score:   Calculated relevance score using the formula:
                           score = (frequency * 10) + 1000 - (depth * 5)
        frequency:         Total occurrence count across all query tokens for this URL.
    """

    url: str
    origin_url: Optional[str]
    depth: int
    relevance_score: float
    frequency: int


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

class SearchEngine:
    """Query engine that reads from a live ``ThreadSafeInvertedIndex``.

    The engine holds *references* to the shared data structures so it always
    reflects the latest state indexed by background crawler workers.

    Args:
        inverted_index: The shared inverted index populated by worker threads.
        metadata_map:   The shared metadata map populated by worker threads.
    """

    def __init__(
        self,
        inverted_index: ThreadSafeInvertedIndex,
        metadata_map: ThreadSafeMetadataMap,
    ) -> None:
        self._index = inverted_index
        self._metadata = metadata_map

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        max_results: int = 20,
    ) -> List[SearchResult]:
        """Execute a multi-word AND query against the inverted index.

        The method is safe to call while crawler worker threads are actively
        writing to the same index and metadata map.

        Args:
            query:       A free-text query string. Multiple words are split on
                         whitespace and combined with AND logic.
            max_results: Maximum number of results to return. Defaults to 20.
                         Pass ``0`` or a negative value to return all matches.

        Returns:
            A list of :class:`SearchResult` namedtuples with relevance_score,
            sorted by relevance_score in descending order (highest score first).

            The list is empty when no query tokens are valid or no URL
            satisfies the AND constraint.

        Notes:
            * Tokens are normalised with
              :meth:`~ThreadSafeInvertedIndex.normalize_term` so the query
              vocabulary is always consistent with the stored index.
            * Relevance score is calculated using: score = (frequency * 10) + 1000 - (depth * 5)
            * Each ``get_all_urls_for_word()`` call holds the index lock only for the copy
              operation.  Intersection runs on detached sets — no lock is held
              across the intersection loop.
        """
        tokens = self._tokenize(query)
        if not tokens:
            logger.debug("search: empty token list after normalisation — returning []")
            return []

        logger.debug("search: tokens=%s", tokens)

        # ------------------------------------------------------------------
        # Step 1 — Fetch posting lists (each call briefly acquires index lock)
        # ------------------------------------------------------------------
        posting_lists: List[Tuple[str, Set[str]]] = []
        for token in tokens:
            urls = self._index.get_all_urls_for_word(token)  # returns a detached set
            if not urls:
                # No document contains this token → AND result must be empty
                logger.debug("search: token %r has no matches — short-circuit", token)
                return []
            posting_lists.append((token, urls))

        # ------------------------------------------------------------------
        # Step 2 — AND intersection, starting from the smallest list
        # ------------------------------------------------------------------
        posting_lists.sort(key=lambda pair: len(pair[1]))
        candidate_urls: Set[str] = posting_lists[0][1]
        for _, url_set in posting_lists[1:]:
            candidate_urls = candidate_urls & url_set
            if not candidate_urls:
                logger.debug("search: intersection became empty — no results")
                return []

        logger.debug("search: %d candidate URL(s) after AND intersection", len(candidate_urls))

        # ------------------------------------------------------------------
        # Step 3 — Build frequency maps for each query token
        # ------------------------------------------------------------------
        token_frequencies: Dict[str, Dict[str, int]] = {}
        for token in tokens:
            token_frequencies[token] = self._index.get_urls(token)  # returns Dict[str, int]

        # ------------------------------------------------------------------
        # Step 4 — Build results with relevance scores
        # ------------------------------------------------------------------
        results: List[SearchResult] = []
        for url in candidate_urls:
            meta = self._metadata.get(url)  # brief lock acquire / release
            
            if meta is None:
                # URL was indexed but metadata was not recorded yet (race).
                # Include with sentinel depth so score is lower
                logger.warning(
                    "search: metadata missing for indexed URL %r — included with depth=999999",
                    url,
                )
                depth = 999_999
                origin_url = None
            else:
                depth = meta.depth
                origin_url = meta.origin_url

            # Calculate relevance_score = (frequency * 10) + 1000 - (depth * 5)
            # Sum frequencies across all query tokens for this URL
            total_frequency = sum(
                token_frequencies[token].get(url, 0) for token in tokens
            )
            relevance_score = (total_frequency * 10) + 1000 - (depth * 5)

            results.append(SearchResult(
                url=url,
                origin_url=origin_url,
                depth=depth,
                relevance_score=relevance_score,
                frequency=total_frequency,
            ))

        # ------------------------------------------------------------------
        # Step 5 — Sort by relevance_score descending (highest first)
        # ------------------------------------------------------------------
        results.sort(key=lambda r: r.relevance_score, reverse=True)

        if max_results and max_results > 0:
            results = results[:max_results]

        logger.info(
            "search: query=%r → %d result(s) (max_results=%d)",
            query,
            len(results),
            max_results,
        )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _tokenize(self, query: str) -> List[str]:
        """Normalise and split a query string into deduplicated tokens.

        Uses ``ThreadSafeInvertedIndex.normalize_term`` to guarantee that the
        token vocabulary matches exactly what the indexer stores.

        Duplicates are removed while preserving first-seen order, so that a
        query like "python python web" behaves identically to "python web".
        """
        seen: Set[str] = set()
        tokens: List[str] = []
        for raw in query.split():
            token = ThreadSafeInvertedIndex.normalize_term(raw)
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
        return tokens


# ---------------------------------------------------------------------------
# Module-level convenience wrapper
# ---------------------------------------------------------------------------

def search(
    query: str,
    inverted_index: ThreadSafeInvertedIndex,
    metadata_map: ThreadSafeMetadataMap,
    max_results: int = 20,
) -> List[Tuple[str, Optional[str], int, float, int]]:
    """Module-level search convenience function.

    Constructs a one-shot :class:`SearchEngine` and delegates to
    :meth:`~SearchEngine.search`.  Useful for scripts and tests that do not
    want to manage a long-lived engine instance.

    Args:
        query:          Free-text query (AND semantics, multi-word supported).
        inverted_index: Shared inverted index populated by crawler workers.
        metadata_map:   Shared metadata map populated by crawler workers.
        max_results:    Upper bound on the number of returned results.

    Returns:
        A list of ``(url, origin_url, depth, relevance_score, frequency)`` tuples, 
        sorted by relevance_score in descending order.

    Example::

        from data_structures import CrawlerState
        from search_engine import search

        state = CrawlerState.load_state("state.json")
        results = search(
            "python concurrency",
            state.inverted_index,
            state.metadata_map,
        )
        for url, origin, depth, score, freq in results:
            print(f"[depth={depth}, score={score}, freq={freq}] {url}  (via {origin})")
    """
    engine = SearchEngine(inverted_index, metadata_map)
    return [
        (r.url, r.origin_url, r.depth, r.relevance_score, r.frequency)
        for r in engine.search(query, max_results=max_results)
    ]