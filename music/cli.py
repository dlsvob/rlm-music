"""
Command-line interface for rlm-music.

Provides three commands that form the pipeline:

  rlm-music crawl "Miles Davis"       — Crawl the influence graph starting from an artist
  rlm-music build-db                  — Build a DuckDB knowledge base from crawled JSON
  rlm-music query "Miles Davis"       — Query an artist's lineage from the knowledge base

Each command maps to a phase in the pipeline, mirroring rlm-pipe's
crawl → build-db → query flow.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from music.core.crawler import GovernorConfig, LineageCrawler
from music.core.db import build_db, query_lineage


# ---------------------------------------------------------------------------
# Default output directory — all data goes here
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_DIR = Path("output")


# ---------------------------------------------------------------------------
# Commands — each function implements one CLI subcommand
# ---------------------------------------------------------------------------

def cmd_crawl(args: argparse.Namespace) -> None:
    """Run the crawl phase: resolve artist, expand influence graph, save JSON.

    Takes an artist name (or MBID), crawls their influence network out to
    the configured depth, and writes artists.json + lineage_edges.json.

    Args:
        args: Parsed CLI arguments (artist, --depth, --max-artists, --output).
    """
    governor = GovernorConfig(
        max_depth=args.depth,
        max_artists=args.max_artists,
        max_api_calls=args.max_api_calls,
    )

    crawler = LineageCrawler(governor=governor)

    # Resolve the artist — try as name first
    artist_input = args.artist
    mbid = crawler.seed_by_name(artist_input)

    if mbid is None:
        print(f"Could not find artist: {artist_input}", file=sys.stderr)
        sys.exit(1)

    print(f"Seed: {crawler.artists[mbid].name} ({mbid})")
    print(f"Crawling with depth={governor.max_depth}, max_artists={governor.max_artists}...")

    crawler.crawl()

    output_dir = Path(args.output)
    crawler.save(output_dir)

    print(f"\nDone: {len(crawler.artists)} artists, {len(crawler.edges)} edges")
    print(f"Output: {output_dir}/")


def cmd_build_db(args: argparse.Namespace) -> None:
    """Run the build-db phase: load crawled JSON into DuckDB.

    Reads artists.json and lineage_edges.json from the output directory,
    creates the schema, and populates the database.

    Args:
        args: Parsed CLI arguments (--output, --db).
    """
    output_dir = Path(args.output)
    db_path = Path(args.db) if args.db else None

    if not (output_dir / "artists.json").exists():
        print(f"No artists.json found in {output_dir}. Run 'crawl' first.", file=sys.stderr)
        sys.exit(1)

    result_path = build_db(output_dir, db_path)
    print(f"Database built: {result_path}")


def cmd_query(args: argparse.Namespace) -> None:
    """Query an artist's lineage from the knowledge base.

    Looks up the artist by name and prints their influence relationships
    in a readable format.

    Args:
        args: Parsed CLI arguments (artist, --db).
    """
    db_path = Path(args.db)

    if not db_path.exists():
        print(f"Database not found: {db_path}. Run 'build-db' first.", file=sys.stderr)
        sys.exit(1)

    lineage = query_lineage(db_path, args.artist)

    if not lineage:
        print(f"No results for: {args.artist}", file=sys.stderr)
        sys.exit(1)

    artist = lineage["artist"]
    print(f"\n{'='*60}")
    print(f"  {artist['name']}")
    if artist.get("genres"):
        print(f"  Genres: {', '.join(artist['genres'])}")
    if artist.get("artist_type"):
        print(f"  Type: {artist['artist_type']}")
    years = ""
    if artist.get("begin_year"):
        years = str(artist["begin_year"])
    if artist.get("end_year"):
        years += f"–{artist['end_year']}"
    elif years:
        years += "–present"
    if years:
        print(f"  Active: {years}")
    print(f"{'='*60}")

    # Helper to print a section of related artists
    def _print_section(title: str, artists: list[dict]) -> None:
        """Print a labeled section of related artists.

        Args:
            title: Section heading (e.g. "Influenced by").
            artists: List of artist dicts to display.
        """
        if not artists:
            return
        print(f"\n  {title}:")
        for a in artists:
            year_info = ""
            if a.get("begin_year"):
                year_info = f" ({a['begin_year']})"
            print(f"    • {a['name']}{year_info}")

    _print_section("Influenced by", lineage["influenced_by"])
    _print_section("Influenced", lineage["influenced"])
    _print_section("Member of", lineage["member_of"])
    _print_section("Members", lineage["members"])
    _print_section("Collaborators", lineage["collaborators"])

    # Also output as JSON if requested
    if args.json:
        print(f"\n{'─'*60}")
        print(json.dumps(lineage, indent=2, default=str))

    print()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all subcommands.

    Returns:
        Configured ArgumentParser with crawl, build-db, and query subcommands.
    """
    parser = argparse.ArgumentParser(
        prog="rlm-music",
        description="Music lineage knowledge graph — crawl, store, and query artist influence networks.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging.",
    )

    subs = parser.add_subparsers(dest="command", required=True)

    # -- crawl ---------------------------------------------------------------
    crawl_p = subs.add_parser("crawl", help="Crawl artist influence graph from MusicBrainz.")
    crawl_p.add_argument("artist", help="Artist name to start from (e.g. 'Miles Davis').")
    crawl_p.add_argument("--depth", type=int, default=2, help="Max hops from seed (default: 2).")
    crawl_p.add_argument("--max-artists", type=int, default=100, help="Max artists to collect (default: 100).")
    crawl_p.add_argument("--max-api-calls", type=int, default=150, help="Max MusicBrainz API calls (default: 150).")
    crawl_p.add_argument("--output", default="output", help="Output directory (default: output/).")

    # -- build-db ------------------------------------------------------------
    build_p = subs.add_parser("build-db", help="Build DuckDB from crawled JSON files.")
    build_p.add_argument("--output", default="output", help="Directory with crawled JSON (default: output/).")
    build_p.add_argument("--db", default=None, help="Database path (default: output/music.duckdb).")

    # -- query ---------------------------------------------------------------
    query_p = subs.add_parser("query", help="Query an artist's lineage from the knowledge base.")
    query_p.add_argument("artist", help="Artist name to look up.")
    query_p.add_argument("--db", default="output/music.duckdb", help="Database path (default: output/music.duckdb).")
    query_p.add_argument("--json", action="store_true", help="Also output raw JSON.")

    # -- serve ---------------------------------------------------------------
    serve_p = subs.add_parser("serve", help="Start the web UI server.")
    serve_p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0).")
    serve_p.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command handler."""
    parser = build_parser()
    args = parser.parse_args()

    # Configure logging based on verbosity flag
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Dispatch to command handler
    if args.command == "serve":
        # Import server lazily to avoid loading FastAPI for CLI-only usage
        from music.server.main import run
        run(host=args.host, port=args.port)
        return

    commands = {
        "crawl": cmd_crawl,
        "build-db": cmd_build_db,
        "query": cmd_query,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
