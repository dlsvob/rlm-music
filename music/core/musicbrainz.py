"""
MusicBrainz API client for fetching artist data and relationships.

MusicBrainz is a free, open music encyclopedia. Its API provides structured
data about artists, releases, recordings, and — critically for us — the
relationships between artists (influences, group membership, collaborations).

API docs: https://musicbrainz.org/doc/MusicBrainz_API
Rate limit: 1 request per second (enforced by this client via sleep).

This client focuses on the endpoints we need for lineage crawling:
  1. Artist lookup by MBID (with relationships included)
  2. Artist search by name (to resolve "Miles Davis" → MBID)
  3. Relationship extraction (influence, member-of, etc.)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

from music.models import Artist, LineageEdge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base URL for the MusicBrainz JSON API (v2)
BASE_URL = "https://musicbrainz.org/ws/2"

# MusicBrainz requires a descriptive User-Agent with contact info.
# Using a generic project identifier — replace with your own if forking.
USER_AGENT = "rlm-music/0.1.0 (https://github.com/rlm-music)"

# Minimum delay between requests to respect MusicBrainz rate limits (1 req/sec).
RATE_LIMIT_DELAY = 1.1

# MusicBrainz relationship types we map to our lineage types.
# Keys are MusicBrainz relationship type names; values are our LineageEdge rel_type.
# The direction depends on "direction" field in the MB response:
#   "forward" means the looked-up artist is the source of the relationship,
#   "backward" means the looked-up artist is the target.
MB_REL_TYPE_MAP = {
    "influenced by":     "influenced_by",     # A was influenced by B
    "member of band":    "member_of",          # A is/was member of B
    "collaboration":     "collaboration",      # A collaborated with B
    "teacher":           "teacher_of",         # A's teacher is B
    "subgroup":          "subgroup",           # A is a subgroup of B
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@dataclass
class MusicBrainzClient:
    """Thin wrapper around the MusicBrainz JSON API.

    Handles rate limiting, User-Agent header, and parsing responses
    into our domain model (Artist, LineageEdge).

    Fields:
        user_agent: The User-Agent string sent with every request.
        _last_request_time: Tracks when we last hit the API to enforce rate limiting.
    """

    user_agent: str = USER_AGENT
    _last_request_time: float = 0.0

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Make a rate-limited GET request to the MusicBrainz API.

        Sleeps if needed to stay within the 1-request-per-second limit.
        Returns the parsed JSON response as a dict.

        Args:
            path: API endpoint path (e.g. "/artist/some-uuid").
            params: Query parameters to include.

        Returns:
            Parsed JSON response dict.

        Raises:
            requests.HTTPError: On 4xx/5xx responses.
        """
        # Enforce rate limit — wait if we're requesting too fast
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)

        url = f"{BASE_URL}{path}"
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        params = params or {}
        params["fmt"] = "json"

        logger.debug("GET %s %s", url, params)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        self._last_request_time = time.time()
        resp.raise_for_status()
        return resp.json()

    # -- Artist lookup -------------------------------------------------------

    def lookup_artist(self, mbid: str) -> dict:
        """Fetch full artist data by MBID, including artist-artist relationships.

        The "artist-rels" inc parameter tells MusicBrainz to include
        relationships with other artists in the response — this is where
        we get influence, membership, and collaboration data.

        Args:
            mbid: MusicBrainz artist UUID (e.g. "561d854a-...").

        Returns:
            Raw MusicBrainz artist dict with 'relations' included.
        """
        return self._get(f"/artist/{mbid}", params={"inc": "artist-rels+genres"})

    def search_artist(self, name: str, limit: int = 5) -> list[dict]:
        """Search for artists by name, returning the top matches.

        Useful for resolving a human-readable name like "Miles Davis"
        into an MBID. Returns multiple results since names can be ambiguous
        (e.g. multiple artists named "Black Flag").

        Args:
            name: Artist name to search for.
            limit: Maximum number of results to return.

        Returns:
            List of artist dicts from MusicBrainz search results.
        """
        data = self._get("/artist", params={"query": name, "limit": limit})
        return data.get("artists", [])

    # -- Parsing helpers -----------------------------------------------------

    def parse_artist(self, data: dict, depth: int = 0, is_seed: bool = False) -> Artist:
        """Convert a MusicBrainz artist dict into our Artist dataclass.

        Extracts the fields we care about and maps MusicBrainz's
        life-span and genre structures into flat fields.

        Args:
            data: Raw MusicBrainz artist dict (from lookup or search).
            depth: How many hops from the seed artist.
            is_seed: Whether this is the initial query artist.

        Returns:
            An Artist instance populated from the MusicBrainz data.
        """
        # MusicBrainz nests birth/death years inside "life-span"
        life_span = data.get("life-span", {})
        begin_year = _parse_year(life_span.get("begin"))
        end_year = _parse_year(life_span.get("end"))

        # Genres come as a list of {"name": "jazz", "count": 5} dicts
        genres = [g["name"] for g in data.get("genres", []) if g.get("name")]

        return Artist(
            mbid=data["id"],
            name=data.get("name", ""),
            sort_name=data.get("sort-name", ""),
            artist_type=data.get("type", ""),
            country=data.get("country", ""),
            begin_year=begin_year,
            end_year=end_year,
            genres=genres,
            disambiguation=data.get("disambiguation", ""),
            depth=depth,
            is_seed=is_seed,
        )

    def parse_relationships(self, data: dict, artist_mbid: str) -> list[LineageEdge]:
        """Extract lineage edges from a MusicBrainz artist response.

        Iterates over the 'relations' array and maps MusicBrainz relationship
        types to our LineageEdge types. Skips relationship types we don't
        care about (e.g. "performance", "recording").

        Direction handling:
          MusicBrainz uses "direction" to indicate which end of the
          relationship the looked-up artist is on:
            - "forward": our artist is the subject (e.g. "A influenced by B")
            - "backward": our artist is the object (e.g. "B influenced by A")

        Args:
            data: Raw MusicBrainz artist dict (must include 'relations').
            artist_mbid: The MBID of the artist we looked up.

        Returns:
            List of LineageEdge instances for recognized relationship types.
        """
        edges: list[LineageEdge] = []
        relations = data.get("relations", [])

        for rel in relations:
            mb_type = rel.get("type", "")
            our_type = MB_REL_TYPE_MAP.get(mb_type)
            if our_type is None:
                # Relationship type we don't track (e.g. "performance", "remix")
                continue

            # The related artist is in rel["artist"] (the "other" end)
            target_data = rel.get("artist", {})
            target_mbid = target_data.get("id", "")
            if not target_mbid or target_mbid == artist_mbid:
                continue

            # Direction determines edge direction
            direction = rel.get("direction", "forward")
            # Parse optional time span for the relationship
            begin = _parse_year(rel.get("begin"))
            end = _parse_year(rel.get("end"))
            attributes = rel.get("attributes", [])

            if direction == "forward":
                # "A influenced by B" — A is source, B is target
                edge = LineageEdge(
                    source_mbid=artist_mbid,
                    target_mbid=target_mbid,
                    rel_type=our_type,
                    attributes=attributes,
                    begin_year=begin,
                    end_year=end,
                )
            else:
                # "backward" — the relationship reads in reverse
                # "B influenced by A" — so A influenced B, meaning B is source
                edge = LineageEdge(
                    source_mbid=target_mbid,
                    target_mbid=artist_mbid,
                    rel_type=our_type,
                    attributes=attributes,
                    begin_year=begin,
                    end_year=end,
                )

            edges.append(edge)

        return edges

    def get_related_artist_ids(self, data: dict, artist_mbid: str) -> list[str]:
        """Return MBIDs of all artists related to the given artist.

        Used by the crawler to discover new artists to expand into.
        Returns only MBIDs for relationship types we care about.

        Args:
            data: Raw MusicBrainz artist dict (must include 'relations').
            artist_mbid: The MBID of the artist we looked up.

        Returns:
            List of MBIDs of related artists (deduplicated).
        """
        seen: set[str] = set()
        result: list[str] = []

        for rel in data.get("relations", []):
            if rel.get("type", "") not in MB_REL_TYPE_MAP:
                continue
            target_mbid = rel.get("artist", {}).get("id", "")
            if target_mbid and target_mbid != artist_mbid and target_mbid not in seen:
                seen.add(target_mbid)
                result.append(target_mbid)

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_year(date_str: Optional[str]) -> Optional[int]:
    """Extract the year from a MusicBrainz date string.

    MusicBrainz dates can be "1926-05-26", "1926-05", "1926", or None.
    We only care about the year for lineage purposes.

    Args:
        date_str: A date string from MusicBrainz, or None.

    Returns:
        The year as an integer, or None if unparseable.
    """
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, IndexError):
        return None
