"""
DuckDB database builder for the music lineage knowledge graph.

Reads the JSON output from the crawler (artists.json, lineage_edges.json)
and loads it into a DuckDB database with a structured schema. This mirrors
rlm-pipe's build-db phase but with music-specific tables.

Schema:
  - artists: Core artist metadata (mbid, name, type, country, years, etc.)
  - artist_genres: Many-to-many relationship between artists and genre tags
  - lineage_edges: Directed relationships between artists (influence, membership, etc.)

The database file (music.duckdb) is the queryable knowledge base that
downstream tools use for lineage lookups.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import duckdb

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema — the CREATE TABLE statements for our music knowledge base
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Artists table: one row per musician, band, or ensemble.
-- mbid (MusicBrainz ID) is the primary key — a stable UUID.
CREATE TABLE IF NOT EXISTS artists (
    mbid             VARCHAR PRIMARY KEY,  -- MusicBrainz UUID
    name             VARCHAR,              -- display name
    sort_name        VARCHAR,              -- alphabetical sort name
    artist_type      VARCHAR,              -- Person, Group, Orchestra, etc.
    country          VARCHAR,              -- ISO 3166-1 alpha-2 code
    begin_year       INTEGER,              -- birth/formation year
    end_year         INTEGER,              -- death/disbandment year
    disambiguation   VARCHAR,              -- note to distinguish same-name artists
    depth            INTEGER,              -- hops from seed artist in crawl
    is_seed          BOOLEAN,              -- true if this was the seed artist
    expanded         BOOLEAN DEFAULT FALSE -- true if we've fetched this artist's full relationships
);

-- Artist genres: many-to-many relationship between artists and genre tags.
-- Stored separately because an artist can have multiple genres.
CREATE TABLE IF NOT EXISTS artist_genres (
    mbid             VARCHAR,              -- FK to artists.mbid
    genre            VARCHAR               -- genre tag name (e.g. "jazz", "bebop")
);

-- Lineage edges: directed relationships between artists.
-- The meaning of source→target depends on rel_type:
--   influenced_by: source was influenced BY target
--   member_of: source is/was a member of target (group)
--   collaboration: source and target worked together
--   teacher_of: source was taught BY target
--   subgroup: source is a subgroup of target
CREATE TABLE IF NOT EXISTS lineage_edges (
    source_mbid      VARCHAR,              -- FK to artists.mbid
    target_mbid      VARCHAR,              -- FK to artists.mbid
    rel_type         VARCHAR,              -- relationship type (see above)
    attributes       VARCHAR,              -- JSON array of extra attributes
    begin_year       INTEGER,              -- when the relationship started
    end_year         INTEGER               -- when the relationship ended
);
"""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_db(output_dir: Path, db_path: Path | None = None) -> Path:
    """Build a DuckDB database from crawled JSON files.

    Reads artists.json and lineage_edges.json from output_dir, creates
    the schema, and inserts all rows. Returns the path to the database.

    Args:
        output_dir: Directory containing artists.json and lineage_edges.json.
        db_path: Where to write the database file. Defaults to output_dir/music.duckdb.

    Returns:
        Path to the created/updated DuckDB file.
    """
    if db_path is None:
        db_path = output_dir / "music.duckdb"

    artists_path = output_dir / "artists.json"
    edges_path = output_dir / "lineage_edges.json"

    # Load JSON data
    with open(artists_path) as f:
        artists = json.load(f)
    with open(edges_path) as f:
        edges = json.load(f)

    logger.info("Building DB from %d artists, %d edges", len(artists), len(edges))

    # Connect and create schema
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_SQL)

    # -- Load artists --------------------------------------------------------
    artist_rows = []
    genre_rows = []

    for a in artists:
        artist_rows.append((
            a["mbid"],
            a.get("name", ""),
            a.get("sort_name", ""),
            a.get("artist_type", ""),
            a.get("country", ""),
            a.get("begin_year"),
            a.get("end_year"),
            a.get("disambiguation", ""),
            a.get("depth", 0),
            a.get("is_seed", False),
            a.get("expanded", False),
        ))

        # Each genre tag gets its own row in artist_genres
        for genre in a.get("genres", []):
            genre_rows.append((a["mbid"], genre))

    if artist_rows:
        con.executemany(
            "INSERT OR IGNORE INTO artists VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            artist_rows,
        )
    if genre_rows:
        con.executemany(
            "INSERT INTO artist_genres VALUES (?, ?)",
            genre_rows,
        )

    # -- Load lineage edges (deduplicated by source+target+type) -------------
    edge_rows = []
    seen_edges: set[tuple[str, str, str]] = set()

    for e in edges:
        key = (e["source_mbid"], e["target_mbid"], e["rel_type"])
        if key in seen_edges:
            continue
        seen_edges.add(key)

        edge_rows.append((
            e["source_mbid"],
            e["target_mbid"],
            e["rel_type"],
            json.dumps(e.get("attributes", [])),  # store as JSON string
            e.get("begin_year"),
            e.get("end_year"),
        ))

    if edge_rows:
        con.executemany(
            "INSERT INTO lineage_edges VALUES (?, ?, ?, ?, ?, ?)",
            edge_rows,
        )

    con.close()
    logger.info("Database written to %s", db_path)
    return db_path


# ---------------------------------------------------------------------------
# Query helpers — convenience functions for common lineage lookups
# ---------------------------------------------------------------------------

def query_lineage(db_path: Path, artist_name: str) -> dict:
    """Look up an artist's full lineage from the knowledge base.

    Returns a dict with:
      - artist: The matched artist's info
      - influenced_by: Artists who influenced them
      - influenced: Artists they influenced
      - member_of: Groups they were part of
      - members: Members (if the artist is a group)
      - collaborators: Artists they collaborated with

    Args:
        db_path: Path to the music.duckdb file.
        artist_name: Name to search for (case-insensitive partial match).

    Returns:
        Dict with lineage data, or empty dict if no match.
    """
    con = duckdb.connect(str(db_path), read_only=True)

    # Find the artist by name (case-insensitive)
    result = con.execute(
        "SELECT * FROM artists WHERE LOWER(name) = LOWER(?) LIMIT 1",
        [artist_name],
    ).fetchone()

    if not result:
        # Try partial match
        result = con.execute(
            "SELECT * FROM artists WHERE LOWER(name) LIKE LOWER(?) LIMIT 1",
            [f"%{artist_name}%"],
        ).fetchone()

    if not result:
        con.close()
        return {}

    # Get column names for building a nice dict
    cols = [desc[0] for desc in con.description]
    artist = dict(zip(cols, result))
    mbid = artist["mbid"]

    # Fetch genres
    genres = con.execute(
        "SELECT genre FROM artist_genres WHERE mbid = ?", [mbid]
    ).fetchall()
    artist["genres"] = [g[0] for g in genres]

    # Fetch lineage edges in both directions
    def _fetch_related(rel_type: str, direction: str) -> list[dict]:
        """Fetch artists connected by a specific relationship type and direction.

        Args:
            rel_type: The relationship type to filter on.
            direction: "outgoing" (source=our artist) or "incoming" (target=our artist).

        Returns:
            List of dicts with related artist info.
        """
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
        "influenced_by": _fetch_related("influenced_by", "outgoing"),
        "influenced": _fetch_related("influenced_by", "incoming"),
        "member_of": _fetch_related("member_of", "outgoing"),
        "members": _fetch_related("member_of", "incoming"),
        "collaborators": _fetch_related("collaboration", "outgoing")
                       + _fetch_related("collaboration", "incoming"),
    }

    con.close()
    return lineage


# ---------------------------------------------------------------------------
# On-demand expansion — fetch an artist's relationships from MusicBrainz
# and merge them into the existing database.
# ---------------------------------------------------------------------------

def expand_artist(db_path: Path, mbid: str) -> bool:
    """Fetch an artist's full data + relationships from MusicBrainz and merge into DB.

    If the artist is already marked as expanded, this is a no-op.
    Otherwise it calls MusicBrainz, inserts any new artists and edges,
    and marks the artist as expanded.

    Args:
        db_path: Path to the music.duckdb file.
        mbid: MusicBrainz UUID of the artist to expand.

    Returns:
        True if new data was fetched, False if already expanded.
    """
    from music.core.musicbrainz import MusicBrainzClient

    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_SQL)  # ensure tables exist

    # Check if already expanded
    row = con.execute("SELECT expanded FROM artists WHERE mbid = ?", [mbid]).fetchone()
    if row and row[0]:
        con.close()
        return False

    client = MusicBrainzClient()

    try:
        data = client.lookup_artist(mbid)
    except Exception as exc:
        logger.warning("Failed to fetch %s from MusicBrainz: %s", mbid, exc)
        con.close()
        return False

    # Parse the full artist data
    full_artist = client.parse_artist(data, depth=0, is_seed=False)

    # Upsert the center artist with full data
    # Delete and re-insert to update all fields (DuckDB has no UPSERT with all cols)
    con.execute("DELETE FROM artists WHERE mbid = ?", [mbid])
    con.execute(
        "INSERT INTO artists VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            full_artist.mbid, full_artist.name, full_artist.sort_name,
            full_artist.artist_type, full_artist.country,
            full_artist.begin_year, full_artist.end_year,
            full_artist.disambiguation, 0, False, True,  # expanded=True
        ),
    )

    # Upsert genres
    con.execute("DELETE FROM artist_genres WHERE mbid = ?", [mbid])
    for genre in full_artist.genres:
        con.execute("INSERT INTO artist_genres VALUES (?, ?)", (mbid, genre))

    # Parse and insert relationships
    edges = client.parse_relationships(data, mbid)
    related_ids = client.get_related_artist_ids(data, mbid)

    # Insert neighbor artists as stubs if they don't exist yet
    for rel in data.get("relations", []):
        rel_artist = rel.get("artist", {})
        rel_mbid = rel_artist.get("id", "")
        if not rel_mbid or rel_mbid == mbid:
            continue
        # Check if this artist already exists
        existing = con.execute("SELECT mbid FROM artists WHERE mbid = ?", [rel_mbid]).fetchone()
        if not existing:
            # Insert as unexpanded stub with whatever name MusicBrainz gave us
            con.execute(
                "INSERT INTO artists VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rel_mbid, rel_artist.get("name", ""), rel_artist.get("sort-name", ""),
                    rel_artist.get("type", ""), "", None, None,
                    rel_artist.get("disambiguation", ""), 0, False, False,
                ),
            )

    # Insert edges (skip duplicates)
    for e in edges:
        existing = con.execute(
            "SELECT 1 FROM lineage_edges WHERE source_mbid = ? AND target_mbid = ? AND rel_type = ?",
            (e.source_mbid, e.target_mbid, e.rel_type),
        ).fetchone()
        if not existing:
            con.execute(
                "INSERT INTO lineage_edges VALUES (?, ?, ?, ?, ?, ?)",
                (e.source_mbid, e.target_mbid, e.rel_type,
                 json.dumps(e.attributes), e.begin_year, e.end_year),
            )

    con.close()
    logger.info("Expanded %s (%s): %d edges, %d related artists",
                full_artist.name, mbid, len(edges), len(related_ids))
    return True


# ---------------------------------------------------------------------------
# Ego-graph query — return the N-hop neighborhood of an artist
# ---------------------------------------------------------------------------

def query_ego(db_path: Path, mbid: str, hops: int = 1) -> dict[str, Any]:
    """Return the ego-graph (neighborhood) of an artist.

    Collects all artists within N hops of the center artist and all
    edges between them. This is the subgraph the frontend renders.

    Args:
        db_path: Path to the music.duckdb file.
        mbid: MusicBrainz UUID of the center artist.
        hops: Number of hops from center to include (default 1).

    Returns:
        Dict with:
          - center: the center artist dict
          - nodes: list of artist dicts in the neighborhood
          - links: list of edge dicts between those artists
    """
    con = duckdb.connect(str(db_path), read_only=True)

    # BFS to find all nodes within N hops
    visited: set[str] = {mbid}
    frontier: set[str] = {mbid}

    for _ in range(hops):
        if not frontier:
            break
        placeholders = ",".join(["?"] * len(frontier))
        frontier_list = list(frontier)

        # Find all neighbors via edges in both directions
        rows = con.execute(f"""
            SELECT DISTINCT target_mbid FROM lineage_edges
            WHERE source_mbid IN ({placeholders})
            UNION
            SELECT DISTINCT source_mbid FROM lineage_edges
            WHERE target_mbid IN ({placeholders})
        """, frontier_list + frontier_list).fetchall()

        next_frontier: set[str] = set()
        for r in rows:
            if r[0] not in visited:
                visited.add(r[0])
                next_frontier.add(r[0])
        frontier = next_frontier

    # Fetch all artists in the neighborhood
    placeholders = ",".join(["?"] * len(visited))
    visited_list = list(visited)

    artist_rows = con.execute(f"""
        SELECT mbid, name, sort_name, artist_type, country,
               begin_year, end_year, disambiguation, depth, is_seed, expanded
        FROM artists
        WHERE mbid IN ({placeholders})
    """, visited_list).fetchall()
    cols = [d[0] for d in con.description]

    nodes = []
    for row in artist_rows:
        node = dict(zip(cols, row))
        # Fetch genres
        genres = con.execute(
            "SELECT genre FROM artist_genres WHERE mbid = ?", [node["mbid"]]
        ).fetchall()
        node["genres"] = [g[0] for g in genres]
        # Count total edges for this artist (including those outside the ego view)
        # so the frontend can show "this node has more connections"
        total_edges = con.execute("""
            SELECT COUNT(*) FROM lineage_edges
            WHERE source_mbid = ? OR target_mbid = ?
        """, [node["mbid"], node["mbid"]]).fetchone()[0]
        node["total_edges"] = total_edges
        nodes.append(node)

    # Fetch all edges between nodes in the neighborhood
    edges = con.execute(f"""
        SELECT source_mbid, target_mbid, rel_type, begin_year, end_year
        FROM lineage_edges
        WHERE source_mbid IN ({placeholders})
          AND target_mbid IN ({placeholders})
    """, visited_list + visited_list).fetchall()

    links = [
        {"source": e[0], "target": e[1], "rel_type": e[2],
         "begin_year": e[3], "end_year": e[4]}
        for e in edges
    ]

    # Find the center artist
    center = next((n for n in nodes if n["mbid"] == mbid), None)

    con.close()
    return {"center": center, "nodes": nodes, "links": links}
