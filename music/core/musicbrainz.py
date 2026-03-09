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

    def fetch_release_groups(self, mbid: str, artist_name: str = "") -> list[dict]:
        """Fetch an artist's release groups (albums) from MusicBrainz.

        Uses the browse endpoint to get all release groups for an artist,
        filtered to albums. Includes URL relationships so we can extract
        Wikipedia links directly from the response.

        Args:
            mbid: MusicBrainz artist UUID.
            artist_name: Artist name, used to construct fallback Wikipedia
                         search URLs when no direct link exists.

        Returns:
            List of dicts with keys: title, year, wikipedia_url, mbid.
            Sorted by year ascending (oldest first), nulls last.
        """
        results = []
        offset = 0
        limit = 100

        # Paginate through all release groups (MusicBrainz caps at 100 per request)
        while True:
            data = self._get("/release-group", params={
                "artist": mbid,
                "type": "album",
                "inc": "url-rels",
                "limit": limit,
                "offset": offset,
            })

            groups = data.get("release-groups", [])
            for rg in groups:
                # Skip compilations, live albums, remix albums, etc.
                # We only want studio albums (primary-type "Album" with no secondary types)
                secondary = rg.get("secondary-types", [])
                if secondary:
                    continue

                title = rg.get("title", "")
                # Extract year from first-release-date (format: "YYYY-MM-DD" or "YYYY")
                year = _parse_year(rg.get("first-release-date"))

                # Look for a Wikipedia URL in the release group's URL relationships.
                # MusicBrainz stores these when editors have linked the release group
                # to its Wikipedia article — the most reliable source.
                wikipedia_url = None
                for rel in rg.get("relations", []):
                    url_resource = rel.get("url", {}).get("resource", "")
                    if "wikipedia.org" in url_resource:
                        wikipedia_url = url_resource
                        break

                results.append({
                    "title": title,
                    "year": year,
                    "wikipedia_url": wikipedia_url,
                    "mbid": rg.get("id", ""),
                })

            # Check if there are more pages
            total = data.get("release-group-count", 0)
            offset += limit
            if offset >= total:
                break

        # For albums without a direct Wikipedia link from MusicBrainz,
        # scrape the artist's Wikipedia page for album links. The discography
        # section (and related "X discography" pages) contain curated links
        # to album articles — much more reliable than per-album search queries.
        needs_lookup = [r for r in results if not r["wikipedia_url"] and r["title"]]
        if needs_lookup:
            resolved = _resolve_from_artist_wikipedia(needs_lookup, artist_name)
            for r in needs_lookup:
                url = resolved.get(r["mbid"])
                if url:
                    r["wikipedia_url"] = url

        # Sort by year ascending, pushing None years to the end
        results.sort(key=lambda r: (r["year"] is None, r["year"] or 0))
        return results

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


# Wikipedia API base URL
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

# User-Agent for Wikipedia API (they request descriptive UAs)
WIKIPEDIA_UA = "rlm-music/0.1.0 (https://github.com/rlm-music)"


def _normalize_for_match(title: str) -> str:
    """Normalize a title for fuzzy matching.

    Lowercases, strips punctuation and whitespace so that minor differences
    between MusicBrainz titles and Wikipedia link text don't prevent matching.
    E.g. "Déjà vu" vs "Déjà Vu" vs "Deja Vu" should all match.

    Args:
        title: The string to normalize.

    Returns:
        Lowercased, stripped version with only alphanumeric + spaces.
    """
    import unicodedata
    # Decompose accents (é → e + combining accent), then strip combining chars
    nfkd = unicodedata.normalize("NFKD", title.lower())
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Remove anything that isn't alphanumeric or space
    return "".join(c for c in stripped if c.isalnum() or c == " ").strip()


def _get_wikipedia_page_links(page_title: str) -> list[dict]:
    """Fetch all internal links from a Wikipedia page.

    Uses the MediaWiki API to get every link on the page. This includes
    links in the discography section, infoboxes, "See also", etc.
    Paginates through all results (Wikipedia returns max 500 at a time).

    Args:
        page_title: Exact Wikipedia article title (e.g. "Miles Davis").

    Returns:
        List of dicts with "title" key for each linked article.
    """
    all_links: list[dict] = []
    params: dict[str, Any] = {
        "action": "query",
        "titles": page_title,
        "prop": "links",
        "pllimit": "max",      # up to 500 per request
        "plnamespace": 0,       # main namespace only
        "format": "json",
    }

    try:
        while True:
            resp = requests.get(
                WIKIPEDIA_API,
                params=params,
                headers={"User-Agent": WIKIPEDIA_UA},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            pages = data.get("query", {}).get("pages", {})
            for page in pages.values():
                all_links.extend(page.get("links", []))

            # Handle pagination — Wikipedia uses "continue" tokens
            cont = data.get("continue")
            if cont:
                params.update(cont)
            else:
                break
    except Exception as exc:
        logger.debug("Failed to get links from Wikipedia page %r: %s", page_title, exc)

    return all_links


def _find_artist_wikipedia_page(artist_name: str) -> Optional[str]:
    """Find the Wikipedia article title for an artist.

    Uses opensearch to resolve the artist name to an actual Wikipedia page
    title (handling redirects, disambiguation, etc.).

    Args:
        artist_name: The artist's name (e.g. "Crosby, Stills, Nash & Young").

    Returns:
        The exact Wikipedia article title, or None if not found.
    """
    try:
        resp = requests.get(
            WIKIPEDIA_API,
            params={
                "action": "opensearch",
                "search": artist_name,
                "limit": 1,
                "namespace": 0,
                "format": "json",
            },
            headers={"User-Agent": WIKIPEDIA_UA},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        titles = data[1] if len(data) > 1 else []
        return titles[0] if titles else None
    except Exception as exc:
        logger.debug("Wikipedia artist search failed for %r: %s", artist_name, exc)
        return None


def _resolve_from_artist_wikipedia(
    albums: list[dict], artist_name: str
) -> dict[str, Optional[str]]:
    """Match album titles against links found on the artist's Wikipedia page.

    Strategy:
      1. Find the artist's Wikipedia page via opensearch.
      2. Scrape all internal links from that page (includes discography links).
      3. Also check for a dedicated "{artist} discography" page, which often
         has more complete album listings than the main article.
      4. For each MusicBrainz album title, fuzzy-match against the collected
         link titles and return the Wikipedia URL for matches.

    This approach is much more reliable than per-album search because:
      - Wikipedia's discography sections are human-curated
      - We get exact article titles (no disambiguation guessing)
      - One or two API calls covers all albums (vs N calls for N albums)

    Args:
        albums: List of album dicts (must have "mbid" and "title" keys).
        artist_name: The artist's display name.

    Returns:
        Dict mapping album MBID → Wikipedia URL for matched albums.
    """
    results: dict[str, Optional[str]] = {}

    # Step 1: find the artist's main Wikipedia page
    artist_page = _find_artist_wikipedia_page(artist_name)
    if not artist_page:
        return results

    # Step 2: collect links from the main artist page
    all_links = _get_wikipedia_page_links(artist_page)

    # Step 3: also check for a dedicated discography page, which often has
    # a more complete list of albums with links to each one.
    # Common patterns: "Miles Davis discography", "The Beatles discography"
    disco_page = _find_artist_wikipedia_page(f"{artist_name} discography")
    if disco_page:
        all_links.extend(_get_wikipedia_page_links(disco_page))

    # Build a lookup: normalized link title → original Wikipedia title
    # We normalize both sides so minor differences don't block matching
    link_map: dict[str, str] = {}
    for link in all_links:
        link_title = link.get("title", "")
        if link_title:
            norm = _normalize_for_match(link_title)
            link_map[norm] = link_title

    # Step 4: match each album against the collected links
    for album in albums:
        album_norm = _normalize_for_match(album["title"])
        if not album_norm:
            continue

        # Try exact normalized match first (covers most cases)
        if album_norm in link_map:
            wiki_title = link_map[album_norm]
            results[album["mbid"]] = (
                "https://en.wikipedia.org/wiki/"
                + requests.utils.quote(wiki_title.replace(" ", "_"))
            )
            continue

        # Try substring match: check if any link title starts with the album title.
        # Handles disambiguation suffixes like "American Dream (album)"
        for norm_link, wiki_title in link_map.items():
            if norm_link.startswith(album_norm) or album_norm.startswith(norm_link):
                results[album["mbid"]] = (
                    "https://en.wikipedia.org/wiki/"
                    + requests.utils.quote(wiki_title.replace(" ", "_"))
                )
                break

    return results
