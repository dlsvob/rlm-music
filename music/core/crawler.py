"""
Lineage graph crawler — expands artist influence networks from a seed.

Given a starting artist (by name or MBID), this crawler fetches their
MusicBrainz relationships, discovers connected artists, and iteratively
expands the graph outward. It's the musical equivalent of rlm-pipe's
citation graph crawler, but following influence/membership edges instead
of citation edges.

Expansion strategy:
  1. Start with the seed artist
  2. Fetch their relationships from MusicBrainz
  3. Add newly-discovered artists to the frontier (priority queue)
  4. Expand the highest-priority frontier artist next
  5. Repeat until a stopping condition is met

Stopping conditions (GovernorConfig):
  - max_artists: Hard cap on total artists in the graph
  - max_depth: Maximum hops from the seed artist
  - max_api_calls: Budget for MusicBrainz API requests

Priority: seed artists first, then by depth (closer = higher priority).
"""

from __future__ import annotations

import heapq
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from music.core.musicbrainz import MusicBrainzClient
from music.models import Artist, LineageEdge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Governor configuration — controls when crawling stops
# ---------------------------------------------------------------------------

@dataclass
class GovernorConfig:
    """Stopping criteria for the lineage crawler.

    These mirror rlm-pipe's GovernorConfig but are tuned for music:
    smaller defaults since music influence graphs are denser than
    citation networks.

    Fields:
        max_depth: Maximum hops from seed (2 = seed's influences + their influences).
        max_artists: Stop after collecting this many artists.
        max_api_calls: Budget for MusicBrainz requests (1 per artist expansion).
    """

    max_depth: int = 2
    max_artists: int = 100
    max_api_calls: int = 150


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class LineageCrawler:
    """Breadth-first crawler that expands an artist influence graph.

    Uses a min-heap priority queue ordered by depth, so closer artists
    are expanded before distant ones. Tracks which artists have been
    seen to avoid cycles (common in music — mutual influences).

    Attributes:
        client: MusicBrainz API client instance.
        governor: Stopping criteria configuration.
        artists: Dict of mbid → Artist for all discovered artists.
        edges: List of all discovered LineageEdge relationships.
        _frontier: Min-heap of (depth, mbid) tuples awaiting expansion.
        _seen: Set of MBIDs we've already added to the graph.
        _api_calls: Running count of API requests made.
    """

    def __init__(self, governor: GovernorConfig | None = None) -> None:
        """Initialize the crawler with optional governor config.

        Args:
            governor: Stopping criteria. Uses defaults if not provided.
        """
        self.client = MusicBrainzClient()
        self.governor = governor or GovernorConfig()

        # Discovered data — populated during crawl
        self.artists: dict[str, Artist] = {}
        self.edges: list[LineageEdge] = []

        # Internal state
        self._frontier: list[tuple[int, str]] = []  # min-heap: (depth, mbid)
        self._seen: set[str] = set()
        self._api_calls = 0

    def seed_by_name(self, name: str) -> str | None:
        """Resolve an artist name to an MBID and add as seed.

        Searches MusicBrainz for the name, takes the top result,
        and adds it to the frontier as the seed artist (depth=0).

        Args:
            name: Human-readable artist name (e.g. "Miles Davis").

        Returns:
            The MBID of the matched artist, or None if no match found.
        """
        results = self.client.search_artist(name, limit=5)
        if not results:
            logger.warning("No MusicBrainz results for %r", name)
            return None

        # Take the best match (MusicBrainz ranks by relevance)
        best = results[0]
        mbid = best["id"]
        logger.info("Resolved %r → %s (%s)", name, best.get("name"), mbid)

        # Parse into Artist and add as seed
        artist = self.client.parse_artist(best, depth=0, is_seed=True)
        self._add_artist(artist)
        return mbid

    def seed_by_mbid(self, mbid: str) -> None:
        """Add an artist by MBID as a seed (depth=0).

        Creates a placeholder Artist and queues it for expansion.
        The full data will be fetched when the artist is expanded.

        Args:
            mbid: MusicBrainz artist UUID.
        """
        if mbid in self._seen:
            return
        # Placeholder — will be filled in during expansion
        artist = Artist(mbid=mbid, depth=0, is_seed=True)
        self._add_artist(artist)

    def crawl(self) -> None:
        """Run the crawl loop until a stopping condition is met.

        Pops the highest-priority (lowest depth) artist from the frontier,
        fetches their relationships from MusicBrainz, adds new artists
        to the graph, and repeats.

        Stops when:
          - Frontier is empty (fully explored within depth limit)
          - max_artists reached
          - max_api_calls reached
        """
        while self._frontier:
            # Check stopping conditions
            if len(self.artists) >= self.governor.max_artists:
                logger.info("Reached max_artists (%d)", self.governor.max_artists)
                break
            if self._api_calls >= self.governor.max_api_calls:
                logger.info("Reached max_api_calls (%d)", self.governor.max_api_calls)
                break

            # Pop the closest unexpanded artist
            depth, mbid = heapq.heappop(self._frontier)
            artist = self.artists.get(mbid)
            if artist is None or artist.expanded:
                continue

            # Expand: fetch full data + relationships from MusicBrainz
            self._expand(artist)

        logger.info(
            "Crawl complete: %d artists, %d edges, %d API calls",
            len(self.artists), len(self.edges), self._api_calls,
        )

    def _add_artist(self, artist: Artist) -> None:
        """Add an artist to the graph and frontier (if within depth limit).

        Skips artists we've already seen. Only queues for expansion
        if the artist's depth is within the governor's max_depth.

        Args:
            artist: The Artist to add.
        """
        if artist.mbid in self._seen:
            return

        self._seen.add(artist.mbid)
        self.artists[artist.mbid] = artist

        # Only queue for expansion if within depth limit
        if artist.depth <= self.governor.max_depth:
            heapq.heappush(self._frontier, (artist.depth, artist.mbid))

    def _expand(self, artist: Artist) -> None:
        """Fetch an artist's full data and relationships, adding new edges and artists.

        Makes one API call to MusicBrainz, parses the response into
        LineageEdges and newly-discovered Artists, and adds them to
        the graph.

        Args:
            artist: The Artist to expand (must not be already expanded).
        """
        logger.info(
            "Expanding [depth=%d] %s (%s)", artist.depth, artist.name or artist.mbid, artist.mbid
        )

        try:
            data = self.client.lookup_artist(artist.mbid)
            self._api_calls += 1
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", artist.mbid, exc)
            artist.expanded = True
            return

        # Update artist with full data (search results have less detail)
        full_artist = self.client.parse_artist(data, depth=artist.depth, is_seed=artist.is_seed)
        full_artist.expanded = True
        self.artists[artist.mbid] = full_artist

        # Extract lineage edges
        new_edges = self.client.parse_relationships(data, artist.mbid)
        self.edges.extend(new_edges)

        # Discover new artists from relationships
        related_ids = self.client.get_related_artist_ids(data, artist.mbid)
        next_depth = artist.depth + 1

        for related_mbid in related_ids:
            if related_mbid not in self._seen:
                # Create a placeholder artist — will get full data when expanded
                new_artist = Artist(mbid=related_mbid, depth=next_depth)

                # Try to get the name from the relationship data for logging
                for rel in data.get("relations", []):
                    rel_artist = rel.get("artist", {})
                    if rel_artist.get("id") == related_mbid:
                        new_artist.name = rel_artist.get("name", "")
                        break

                self._add_artist(new_artist)

        logger.info(
            "  → %d new edges, %d new artists discovered",
            len(new_edges), sum(1 for rid in related_ids if rid not in self._seen),
        )

    # -- Serialization -------------------------------------------------------

    def save(self, output_dir: Path) -> None:
        """Write crawled data to JSON files in the output directory.

        Creates two files:
          - artists.json: Array of artist dicts (one per discovered artist)
          - lineage_edges.json: Array of edge dicts (all relationships)

        Args:
            output_dir: Directory to write files into (created if needed).
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        artists_path = output_dir / "artists.json"
        edges_path = output_dir / "lineage_edges.json"

        # Convert dataclasses to dicts for JSON serialization
        artists_data = [asdict(a) for a in self.artists.values()]
        edges_data = [asdict(e) for e in self.edges]

        with open(artists_path, "w") as f:
            json.dump(artists_data, f, indent=2)
        logger.info("Wrote %d artists to %s", len(artists_data), artists_path)

        with open(edges_path, "w") as f:
            json.dump(edges_data, f, indent=2)
        logger.info("Wrote %d edges to %s", len(edges_data), edges_path)
