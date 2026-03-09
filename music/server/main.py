"""
FastAPI web server for the music lineage knowledge graph.

Serves both the REST API and the static frontend. The API provides
endpoints for searching artists, triggering crawls, and querying
the lineage graph. Crawls run in a background thread with progress
streamed via Server-Sent Events (SSE).

Architecture:
  - GET  /                        → serves the single-page frontend
  - GET  /api/search?q=...        → search MusicBrainz for an artist
  - POST /api/crawl               → start a background crawl from a seed artist
  - GET  /api/crawl/status        → SSE stream of crawl progress
  - GET  /api/graph               → full graph (nodes + edges) for D3 visualization
  - GET  /api/artist/{mbid}       → single artist detail + lineage
  - GET  /api/stats               → summary counts (artists, edges, genres)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from sse_starlette.sse import EventSourceResponse

from music.core.crawler import GovernorConfig, LineageCrawler
from music.core.db import build_db, expand_artist, query_ego, SCHEMA_SQL
from music.core.musicbrainz import MusicBrainzClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

# Path to the static frontend files (served at /static/)
STATIC_DIR = Path(__file__).parent.parent / "static"

# Default output directory and database path
OUTPUT_DIR = Path("output")
DB_PATH = OUTPUT_DIR / "music.duckdb"

app = FastAPI(title="rlm-music", description="Music lineage knowledge graph")


# Middleware to prevent aggressive caching of static assets.
# Mobile browsers (especially iOS Safari) cache aggressively and won't
# pick up JS/CSS changes without this.
class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(NoCacheStaticMiddleware)

# Mount static files (CSS, JS) at /static/
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Crawl state — tracks background crawl progress
# ---------------------------------------------------------------------------

class CrawlState:
    """Shared mutable state for the background crawl.

    The crawl runs in a separate thread. This object lets the API
    endpoints check status and stream progress events to the frontend.

    Attributes:
        running: True while a crawl is in progress.
        events: List of progress event dicts (appended by the crawl thread).
        error: Error message if the crawl failed, else None.
        done: True once the crawl (and DB build) has finished successfully.
    """

    def __init__(self) -> None:
        self.running = False
        self.events: list[dict[str, Any]] = []
        self.error: str | None = None
        self.done = False
        self._lock = threading.Lock()

    def push_event(self, event: dict[str, Any]) -> None:
        """Thread-safe append of a progress event.

        Args:
            event: Dict with at least a "type" key (e.g. "expanding", "complete").
        """
        with self._lock:
            self.events.append(event)

    def get_events_since(self, index: int) -> list[dict[str, Any]]:
        """Return events from the given index onward (thread-safe).

        Args:
            index: Start reading from this position in the events list.

        Returns:
            List of events from index to the current end.
        """
        with self._lock:
            return self.events[index:]

    def reset(self) -> None:
        """Clear state for a new crawl."""
        self.running = False
        self.events = []
        self.error = None
        self.done = False


# Single global crawl state — only one crawl at a time
crawl_state = CrawlState()


# ---------------------------------------------------------------------------
# Background crawl runner
# ---------------------------------------------------------------------------

def _run_crawl(
    artist_name: str,
    max_depth: int,
    max_artists: int,
    max_api_calls: int,
) -> None:
    """Execute a crawl in a background thread, posting progress events.

    This function is the target for threading.Thread. It:
      1. Searches MusicBrainz for the artist
      2. Runs the lineage crawler with progress callbacks
      3. Saves JSON output
      4. Builds the DuckDB database
      5. Posts "complete" or "error" event

    Args:
        artist_name: Human-readable artist name to seed the crawl.
        max_depth: Maximum hops from seed artist.
        max_artists: Maximum artists to collect.
        max_api_calls: Budget for MusicBrainz API requests.
    """
    try:
        crawl_state.push_event({"type": "searching", "artist": artist_name})

        governor = GovernorConfig(
            max_depth=max_depth,
            max_artists=max_artists,
            max_api_calls=max_api_calls,
        )
        crawler = LineageCrawler(governor=governor)

        # Resolve artist name → MBID
        mbid = crawler.seed_by_name(artist_name)
        if mbid is None:
            crawl_state.error = f"Could not find artist: {artist_name}"
            crawl_state.push_event({"type": "error", "message": crawl_state.error})
            crawl_state.running = False
            return

        seed = crawler.artists[mbid]
        crawl_state.push_event({
            "type": "seed_found",
            "name": seed.name,
            "mbid": mbid,
        })

        # Monkey-patch the crawler's _expand to post progress events.
        # We wrap the original method so each expansion generates an SSE event.
        original_expand = crawler._expand

        def _expand_with_events(artist):
            """Wrapper that posts progress before each artist expansion."""
            crawl_state.push_event({
                "type": "expanding",
                "name": artist.name or artist.mbid,
                "depth": artist.depth,
                "total_artists": len(crawler.artists),
                "total_edges": len(crawler.edges),
            })
            original_expand(artist)

        crawler._expand = _expand_with_events

        # Run the crawl
        crawler.crawl()

        # Save JSON output
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        crawler.save(OUTPUT_DIR)

        crawl_state.push_event({
            "type": "building_db",
            "total_artists": len(crawler.artists),
            "total_edges": len(crawler.edges),
        })

        # Build DuckDB
        build_db(OUTPUT_DIR, DB_PATH)

        crawl_state.done = True
        crawl_state.push_event({
            "type": "complete",
            "total_artists": len(crawler.artists),
            "total_edges": len(crawler.edges),
        })

    except Exception as exc:
        logger.exception("Crawl failed")
        crawl_state.error = str(exc)
        crawl_state.push_event({"type": "error", "message": str(exc)})
    finally:
        crawl_state.running = False


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page frontend."""
    index_path = STATIC_DIR / "index.html"
    return HTMLResponse(index_path.read_text())


@app.get("/api/search")
async def search_artist(q: str = Query(..., min_length=1)):
    """Search MusicBrainz for artists matching the query string.

    Returns top 5 matches with name, MBID, type, and disambiguation.

    Args:
        q: Search query (artist name or partial name).

    Returns:
        JSON list of matching artist dicts.
    """
    client = MusicBrainzClient()
    results = client.search_artist(q, limit=5)
    return [
        {
            "mbid": r["id"],
            "name": r.get("name", ""),
            "type": r.get("type", ""),
            "country": r.get("country", ""),
            "disambiguation": r.get("disambiguation", ""),
            "score": r.get("score", 0),
        }
        for r in results
    ]


@app.post("/api/crawl")
async def start_crawl(
    artist: str = Query(..., min_length=1),
    depth: int = Query(2, ge=1, le=5),
    max_artists: int = Query(100, ge=10, le=500),
):
    """Start a background crawl from the given artist.

    Only one crawl can run at a time. Returns immediately with status.

    Args:
        artist: Artist name to seed the crawl.
        depth: Maximum hops from seed (1–5).
        max_artists: Maximum artists to collect (10–500).

    Returns:
        JSON with status "started" or error if already running.
    """
    if crawl_state.running:
        raise HTTPException(status_code=409, detail="A crawl is already running")

    crawl_state.reset()
    crawl_state.running = True

    # Launch crawl in background thread
    thread = threading.Thread(
        target=_run_crawl,
        args=(artist, depth, max_artists, max_artists * 2),
        daemon=True,
    )
    thread.start()

    return {"status": "started", "artist": artist, "depth": depth, "max_artists": max_artists}


@app.get("/api/crawl/status")
async def crawl_status_sse():
    """Stream crawl progress via Server-Sent Events.

    The frontend connects to this endpoint and receives real-time
    updates as the crawl discovers artists and edges. Each event
    is a JSON dict with a "type" field.

    Event types:
      - searching: Looking up artist on MusicBrainz
      - seed_found: Seed artist resolved (includes name + mbid)
      - expanding: About to expand an artist (includes name, depth, totals)
      - building_db: Crawl done, building DuckDB
      - complete: All done (includes final totals)
      - error: Something went wrong (includes message)

    Returns:
        SSE event stream.
    """
    async def event_generator():
        """Yields SSE events as the crawl progresses."""
        index = 0
        while True:
            events = crawl_state.get_events_since(index)
            for event in events:
                yield {"event": "progress", "data": json.dumps(event)}
                index += 1

                # Stop streaming after terminal events
                if event["type"] in ("complete", "error"):
                    return

            # If crawl isn't running and we've seen all events, stop
            if not crawl_state.running and index >= len(crawl_state.events):
                return

            await asyncio.sleep(0.3)

    return EventSourceResponse(event_generator())


@app.get("/api/graph")
async def get_graph():
    """Return the full graph as nodes + links for D3 force visualization.

    Reads from the DuckDB database and returns:
      - nodes: Array of artist objects with mbid, name, type, genres, etc.
      - links: Array of edge objects with source, target, rel_type

    Returns:
        JSON with "nodes" and "links" arrays, or error if DB doesn't exist.
    """
    if not DB_PATH.exists():
        return {"nodes": [], "links": []}

    con = duckdb.connect(str(DB_PATH), read_only=True)

    # Fetch all artists
    artists = con.execute("""
        SELECT mbid, name, sort_name, artist_type, country,
               begin_year, end_year, disambiguation, depth, is_seed
        FROM artists
        ORDER BY depth, name
    """).fetchall()
    cols = [d[0] for d in con.description]

    nodes = []
    for row in artists:
        node = dict(zip(cols, row))
        # Fetch genres for this artist
        genres = con.execute(
            "SELECT genre FROM artist_genres WHERE mbid = ?", [node["mbid"]]
        ).fetchall()
        node["genres"] = [g[0] for g in genres]
        nodes.append(node)

    # Fetch all edges
    edges = con.execute("""
        SELECT source_mbid, target_mbid, rel_type, begin_year, end_year
        FROM lineage_edges
    """).fetchall()

    links = [
        {
            "source": e[0],
            "target": e[1],
            "rel_type": e[2],
            "begin_year": e[3],
            "end_year": e[4],
        }
        for e in edges
    ]

    con.close()
    return {"nodes": nodes, "links": links}


@app.get("/api/artist/{mbid}/releases")
async def get_artist_releases(mbid: str):
    """Fetch an artist's albums (release groups) from MusicBrainz.

    Calls the MusicBrainz browse API for release groups, extracts
    Wikipedia URLs, and returns a sorted list. Used by the frontend
    to show discography under each artist in the detail panel.

    Wrapped in asyncio.to_thread because MusicBrainzClient._get() uses
    time.sleep for rate limiting, which would block the async event loop.

    Args:
        mbid: MusicBrainz UUID of the artist.

    Returns:
        JSON list of release group dicts with title, year, wikipedia_url, mbid.
    """
    # Try to get the artist name from our DB (for better Wikipedia fallback URLs)
    artist_name = ""
    if DB_PATH.exists():
        con = duckdb.connect(str(DB_PATH), read_only=True)
        row = con.execute("SELECT name FROM artists WHERE mbid = ?", [mbid]).fetchone()
        con.close()
        if row:
            artist_name = row[0]

    client = MusicBrainzClient()

    # Run in a thread to avoid blocking the async event loop during rate-limit sleeps
    releases = await asyncio.to_thread(client.fetch_release_groups, mbid, artist_name)
    return releases


@app.get("/api/artist/{mbid}")
async def get_artist(mbid: str):
    """Get detailed info for a single artist, including lineage relationships.

    Returns the artist's metadata plus lists of related artists grouped
    by relationship type (influenced_by, influenced, member_of, etc.).

    Args:
        mbid: MusicBrainz UUID of the artist to look up.

    Returns:
        JSON with artist info and lineage lists.

    Raises:
        HTTPException 404 if artist not found in database.
    """
    if not DB_PATH.exists():
        raise HTTPException(status_code=404, detail="Database not found. Run a crawl first.")

    con = duckdb.connect(str(DB_PATH), read_only=True)

    # Get the artist
    result = con.execute("SELECT * FROM artists WHERE mbid = ?", [mbid]).fetchone()
    if not result:
        con.close()
        raise HTTPException(status_code=404, detail=f"Artist {mbid} not found")

    cols = [d[0] for d in con.description]
    artist = dict(zip(cols, result))

    # Get genres
    genres = con.execute("SELECT genre FROM artist_genres WHERE mbid = ?", [mbid]).fetchall()
    artist["genres"] = [g[0] for g in genres]

    # Helper to fetch related artists by relationship type and direction
    def fetch_related(rel_type: str, direction: str) -> list[dict]:
        if direction == "outgoing":
            rows = con.execute("""
                SELECT a.name, a.mbid, a.artist_type, a.begin_year, a.end_year
                FROM lineage_edges e
                JOIN artists a ON a.mbid = e.target_mbid
                WHERE e.source_mbid = ? AND e.rel_type = ?
            """, [mbid, rel_type]).fetchall()
        else:
            rows = con.execute("""
                SELECT a.name, a.mbid, a.artist_type, a.begin_year, a.end_year
                FROM lineage_edges e
                JOIN artists a ON a.mbid = e.source_mbid
                WHERE e.target_mbid = ? AND e.rel_type = ?
            """, [mbid, rel_type]).fetchall()

        return [
            {"name": r[0], "mbid": r[1], "type": r[2], "begin_year": r[3], "end_year": r[4]}
            for r in rows
        ]

    lineage = {
        "artist": artist,
        "influenced_by": fetch_related("influenced_by", "outgoing"),
        "influenced": fetch_related("influenced_by", "incoming"),
        "member_of": fetch_related("member_of", "outgoing"),
        "members": fetch_related("member_of", "incoming"),
        "collaborators": (
            fetch_related("collaboration", "outgoing")
            + fetch_related("collaboration", "incoming")
        ),
        "taught_by": fetch_related("teacher_of", "outgoing"),
        "students": fetch_related("teacher_of", "incoming"),
    }

    con.close()
    return lineage


@app.get("/api/seed")
async def get_seed():
    """Return just the seed artist's MBID and name.

    Lightweight endpoint used on initial page load to find the starting
    artist without fetching the entire graph. Returns null if no data exists.
    """
    if not DB_PATH.exists():
        return {"seed": None}

    con = duckdb.connect(str(DB_PATH), read_only=True)
    row = con.execute("SELECT mbid, name FROM artists WHERE is_seed = TRUE LIMIT 1").fetchone()
    con.close()

    if row:
        return {"seed": {"mbid": row[0], "name": row[1]}}
    return {"seed": None}


@app.get("/api/stats")
async def get_stats():
    """Return summary statistics about the knowledge base.

    Returns:
        JSON with counts of artists, edges, genres, and relationship type breakdown.
    """
    if not DB_PATH.exists():
        return {"artists": 0, "edges": 0, "genres": 0, "has_data": False}

    con = duckdb.connect(str(DB_PATH), read_only=True)

    artist_count = con.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    edge_count = con.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
    genre_count = con.execute("SELECT COUNT(DISTINCT genre) FROM artist_genres").fetchone()[0]
    named_count = con.execute("SELECT COUNT(*) FROM artists WHERE name != ''").fetchone()[0]

    # Breakdown by relationship type
    rel_types = con.execute(
        "SELECT rel_type, COUNT(*) FROM lineage_edges GROUP BY rel_type ORDER BY COUNT(*) DESC"
    ).fetchall()

    con.close()

    return {
        "artists": artist_count,
        "artists_named": named_count,
        "edges": edge_count,
        "genres": genre_count,
        "rel_types": {r[0]: r[1] for r in rel_types},
        "has_data": artist_count > 0,
    }


@app.get("/api/ego/{mbid}")
async def get_ego(mbid: str, hops: int = Query(1, ge=1, le=3)):
    """Return the ego-graph (N-hop neighborhood) of an artist.

    If the artist hasn't been expanded yet, fetches their relationships
    from MusicBrainz on demand and merges into the database before
    returning the neighborhood.

    Args:
        mbid: MusicBrainz UUID of the center artist.
        hops: Number of hops from center (1–3, default 1).

    Returns:
        JSON with center artist, neighborhood nodes, and links between them.
        Each node includes a total_edges count so the frontend can indicate
        that unexpanded nodes have more connections than shown.
    """
    if not DB_PATH.exists():
        # No DB yet — create it, expand this single artist, then return ego
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(DB_PATH))
        con.execute(SCHEMA_SQL)
        con.close()

    # Expand the center artist on demand if not yet expanded
    expand_artist(DB_PATH, mbid)

    result = query_ego(DB_PATH, mbid, hops=hops)

    if not result["center"]:
        raise HTTPException(status_code=404, detail=f"Artist {mbid} not found")

    return result


@app.post("/api/expand/{mbid}")
async def do_expand(mbid: str):
    """Explicitly expand an artist — fetch their relationships from MusicBrainz.

    Used when the user taps an unexpanded neighbor node. After expansion
    the frontend re-fetches the ego-graph to show the new data.

    Args:
        mbid: MusicBrainz UUID of the artist to expand.

    Returns:
        JSON with expanded status.
    """
    if not DB_PATH.exists():
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(DB_PATH))
        con.execute(SCHEMA_SQL)
        con.close()

    was_new = expand_artist(DB_PATH, mbid)
    return {"expanded": True, "was_new": was_new, "mbid": mbid}


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the uvicorn server.

    Args:
        host: Bind address.
        port: Bind port.
    """
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
