"""
Data model for the music lineage knowledge graph.

Defines the core entities (Artist, Work, LineageEdge) as dataclasses.
These mirror the Paper dataclass from rlm-pipe but are tailored for
music domain: artists instead of papers, influence relationships instead
of citation edges.

Lineage edge types capture the different ways artists relate:
  - "influenced_by": Artist B shaped Artist A's style or sound
  - "member_of": Artist A was a member of group B
  - "collaboration": Artist A and B worked together
  - "teacher_of": Artist A formally taught Artist B
  - "subgroup": Band B spun off from Band A

These are directional — source is the artist being described, target is
the related artist. For "influenced_by", source was influenced BY target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Lineage relationship types — the kinds of edges in our influence graph.
# These are the MusicBrainz artist-artist relationship types we care about,
# mapped to simpler names. We can expand this set later.
# ---------------------------------------------------------------------------

LINEAGE_TYPES = {
    "influenced_by",       # stylistic/artistic influence
    "member_of",           # membership in a group
    "collaboration",       # worked together on a project
    "teacher_of",          # formal mentorship or instruction
    "subgroup",            # band spun off from another band
}


# ---------------------------------------------------------------------------
# Artist — a musician, band, or ensemble
# ---------------------------------------------------------------------------

@dataclass
class Artist:
    """A single musician, band, or musical ensemble.

    The mbid (MusicBrainz ID) is the primary key — a stable UUID that
    uniquely identifies the artist across the MusicBrainz database.

    Fields:
        mbid: MusicBrainz UUID string (primary key).
        name: Display name (e.g. "Miles Davis", "The Beatles").
        sort_name: Name for alphabetical sorting (e.g. "Davis, Miles").
        artist_type: "Person", "Group", "Orchestra", "Choir", "Character", "Other".
        country: ISO 3166-1 alpha-2 country code (e.g. "US", "GB").
        begin_year: Year the artist started (birth year for person, formation for group).
        end_year: Year the artist ended (death year for person, disbandment for group).
        genres: Genre tags from MusicBrainz (e.g. ["jazz", "bebop", "hard bop"]).
        disambiguation: Short note to distinguish same-name artists.
        depth: How many hops from the seed artist this was discovered.
        is_seed: True if this was the initial query artist.
    """

    mbid: str
    name: str = ""
    sort_name: str = ""
    artist_type: str = ""           # Person, Group, Orchestra, etc.
    country: str = ""               # ISO country code
    begin_year: Optional[int] = None
    end_year: Optional[int] = None
    genres: list[str] = field(default_factory=list)
    disambiguation: str = ""

    # -- Graph traversal metadata --
    depth: int = 0                  # hops from seed artist
    is_seed: bool = False           # was this the starting artist?
    expanded: bool = False          # have we fetched this artist's relationships?


# ---------------------------------------------------------------------------
# LineageEdge — a directional relationship between two artists
# ---------------------------------------------------------------------------

@dataclass
class LineageEdge:
    """A directed relationship between two artists.

    The direction matters:
      - For "influenced_by": source was influenced BY target.
      - For "member_of": source is/was a member of target (the group).
      - For "collaboration": bidirectional, but stored once with source < target.
      - For "teacher_of": source taught target.
      - For "subgroup": source is a subgroup of target.

    Fields:
        source_mbid: The artist this edge originates from.
        target_mbid: The artist this edge points to.
        rel_type: One of the LINEAGE_TYPES strings.
        attributes: Extra info from MusicBrainz (e.g. time period, instrument).
        begin_year: When the relationship started (if known).
        end_year: When the relationship ended (if known).
    """

    source_mbid: str
    target_mbid: str
    rel_type: str                   # one of LINEAGE_TYPES
    attributes: list[str] = field(default_factory=list)
    begin_year: Optional[int] = None
    end_year: Optional[int] = None


# ---------------------------------------------------------------------------
# Work — a song, album, or composition (stub for future expansion)
# ---------------------------------------------------------------------------

@dataclass
class Work:
    """A musical work (song, composition, album).

    Stubbed out for now — lineage v1 focuses on artist-to-artist
    relationships. We'll flesh this out when adding work-level lineage
    (covers, samples, arrangements).

    Fields:
        mbid: MusicBrainz work UUID.
        title: Name of the work.
        work_type: "Song", "Album", "Composition", "Opera", etc.
        artist_mbid: The primary artist associated with this work.
    """

    mbid: str
    title: str = ""
    work_type: str = ""
    artist_mbid: str = ""
