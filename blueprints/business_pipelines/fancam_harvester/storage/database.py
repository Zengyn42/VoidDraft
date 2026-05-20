"""
FancamHarvester — SQLite persistence layer.

Tables: posts, clips, upvote_log, metadata_training_log, crawl_log

WAL mode is enabled on every connection to support concurrent readers +
the single cron writer without blocking.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

# ---------------------------------------------------------------------------
# Default DB path (overridden by FancamConfig)
# ---------------------------------------------------------------------------
_DEFAULT_DB = Path.home() / "Foundation" / "EdenGateway" / "RedditPulsify" / "fancam.db"


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS posts (
    post_id         TEXT PRIMARY KEY,
    subreddit       TEXT NOT NULL,
    title           TEXT,
    created_utc     REAL NOT NULL,
    reddit_url      TEXT,
    score           INTEGER DEFAULT 0,
    group_name      TEXT,
    performer       TEXT,
    song            TEXT,
    perf_date       TEXT,
    llm_confidence  REAL,
    crawled_at      REAL NOT NULL,
    settled         INTEGER DEFAULT 0,
    final_score     INTEGER
);

CREATE TABLE IF NOT EXISTS clips (
    clip_id             TEXT PRIMARY KEY,
    post_id             TEXT NOT NULL REFERENCES posts(post_id),
    pixeldrain_filename TEXT,
    clip_type           TEXT,
    local_path          TEXT,
    width               INTEGER,
    height              INTEGER,
    fps                 REAL,
    duration_sec        REAL,

    is_slowmo           INTEGER DEFAULT 0,
    speed_factor        REAL    DEFAULT 1.0,
    is_zoom_in          INTEGER DEFAULT 0,
    zoom_factor         REAL    DEFAULT 1.0,
    zoom_method         TEXT,

    align_method        TEXT,
    align_offset_sec    REAL,
    align_confidence    REAL,
    align_audio_conf    REAL,
    align_dinov2_conf   REAL,
    align_pose_conf     REAL,
    source_clip_id      TEXT,

    final_path          TEXT,
    final_creative_path TEXT,
    final_kept          TEXT
);

CREATE TABLE IF NOT EXISTS upvote_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL REFERENCES posts(post_id),
    recorded_at     REAL NOT NULL,
    post_age_hours  REAL NOT NULL,
    score           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_training_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         TEXT NOT NULL,
    raw_title       TEXT,
    filename        TEXT,
    yt_title        TEXT,
    rule_result     TEXT,
    llm_result      TEXT,
    llm_confidence  REAL,
    recorded_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          REAL NOT NULL,
    posts_seen      INTEGER DEFAULT 0,
    posts_new       INTEGER DEFAULT 0,
    posts_updated   INTEGER DEFAULT 0,
    clips_new       INTEGER DEFAULT 0,
    album_api_calls INTEGER DEFAULT 0,
    errors          TEXT
);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def get_connection(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Yield a WAL-mode SQLite connection with row_factory set."""
    path = Path(db_path) if db_path else _DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    """Create all tables (idempotent)."""
    with get_connection(db_path) as conn:
        conn.executescript(_DDL)


# ---------------------------------------------------------------------------
# posts table
# ---------------------------------------------------------------------------

def upsert_post(
    conn: sqlite3.Connection,
    *,
    post_id: str,
    subreddit: str,
    title: str,
    created_utc: float,
    reddit_url: str = "",
    score: int = 0,
    group_name: str = "",
    performer: str = "",
    song: str = "",
    perf_date: str = "",
    llm_confidence: float | None = None,
    crawled_at: float | None = None,
) -> None:
    """Insert or update a post row. settled/final_score are NOT touched here."""
    now = crawled_at or time.time()
    conn.execute(
        """
        INSERT INTO posts
            (post_id, subreddit, title, created_utc, reddit_url, score,
             group_name, performer, song, perf_date, llm_confidence, crawled_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(post_id) DO UPDATE SET
            score         = excluded.score,
            group_name    = COALESCE(excluded.group_name, group_name),
            performer     = COALESCE(excluded.performer,  performer),
            song          = COALESCE(excluded.song,       song),
            perf_date     = COALESCE(excluded.perf_date,  perf_date),
            llm_confidence= COALESCE(excluded.llm_confidence, llm_confidence)
        """,
        (post_id, subreddit, title, created_utc, reddit_url, score,
         group_name or None, performer or None, song or None,
         perf_date or None, llm_confidence, now),
    )


def post_exists(conn: sqlite3.Connection, post_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM posts WHERE post_id=?", (post_id,)).fetchone()
    return row is not None


def get_unsettled_posts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts WHERE settled=0"
    ).fetchall()


def settle_post(conn: sqlite3.Connection, post_id: str, final_score: int) -> None:
    conn.execute(
        "UPDATE posts SET settled=1, final_score=? WHERE post_id=?",
        (final_score, post_id),
    )


# ---------------------------------------------------------------------------
# clips table
# ---------------------------------------------------------------------------

def upsert_clip(
    conn: sqlite3.Connection,
    *,
    clip_id: str,
    post_id: str,
    pixeldrain_filename: str = "",
    clip_type: str = "",
    local_path: str = "",
    width: int = 0,
    height: int = 0,
    fps: float = 0.0,
    duration_sec: float = 0.0,
    is_slowmo: bool = False,
    speed_factor: float = 1.0,
    is_zoom_in: bool = False,
    zoom_factor: float = 1.0,
    zoom_method: str = "",
    align_method: str = "",
    align_offset_sec: float | None = None,
    align_confidence: float | None = None,
    align_audio_conf: float | None = None,
    align_dinov2_conf: float | None = None,
    align_pose_conf: float | None = None,
    source_clip_id: str = "",
    final_path: str = "",
    final_creative_path: str = "",
    final_kept: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO clips (
            clip_id, post_id, pixeldrain_filename, clip_type, local_path,
            width, height, fps, duration_sec,
            is_slowmo, speed_factor, is_zoom_in, zoom_factor, zoom_method,
            align_method, align_offset_sec, align_confidence,
            align_audio_conf, align_dinov2_conf, align_pose_conf,
            source_clip_id, final_path, final_creative_path, final_kept
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(clip_id) DO UPDATE SET
            local_path          = excluded.local_path,
            is_slowmo           = excluded.is_slowmo,
            speed_factor        = excluded.speed_factor,
            is_zoom_in          = excluded.is_zoom_in,
            zoom_factor         = excluded.zoom_factor,
            zoom_method         = excluded.zoom_method,
            align_method        = excluded.align_method,
            align_offset_sec    = excluded.align_offset_sec,
            align_confidence    = excluded.align_confidence,
            align_audio_conf    = excluded.align_audio_conf,
            align_dinov2_conf   = excluded.align_dinov2_conf,
            align_pose_conf     = excluded.align_pose_conf,
            source_clip_id      = excluded.source_clip_id,
            final_path          = excluded.final_path,
            final_creative_path = excluded.final_creative_path,
            final_kept          = excluded.final_kept
        """,
        (
            clip_id, post_id, pixeldrain_filename or None, clip_type, local_path,
            width, height, fps, duration_sec,
            int(is_slowmo), speed_factor, int(is_zoom_in), zoom_factor,
            zoom_method or None,
            align_method or None, align_offset_sec, align_confidence,
            align_audio_conf, align_dinov2_conf, align_pose_conf,
            source_clip_id or None,
            final_path or None, final_creative_path or None, final_kept or None,
        ),
    )


def get_clip_filenames_for_post(conn: sqlite3.Connection, post_id: str) -> set[str]:
    """Return all known pixeldrain_filename values for a post (for album diff)."""
    rows = conn.execute(
        "SELECT pixeldrain_filename FROM clips WHERE post_id=? AND pixeldrain_filename IS NOT NULL",
        (post_id,),
    ).fetchall()
    return {r["pixeldrain_filename"] for r in rows}


# ---------------------------------------------------------------------------
# upvote_log table
# ---------------------------------------------------------------------------

def log_upvote(
    conn: sqlite3.Connection,
    *,
    post_id: str,
    score: int,
    post_age_hours: float,
) -> None:
    conn.execute(
        "INSERT INTO upvote_log (post_id, recorded_at, post_age_hours, score) VALUES (?,?,?,?)",
        (post_id, time.time(), post_age_hours, score),
    )


# ---------------------------------------------------------------------------
# metadata_training_log table
# ---------------------------------------------------------------------------

def log_metadata_training(
    conn: sqlite3.Connection,
    *,
    post_id: str,
    raw_title: str = "",
    filename: str = "",
    yt_title: str = "",
    rule_result: dict | None = None,
    llm_result: dict | None = None,
    llm_confidence: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO metadata_training_log
            (post_id, raw_title, filename, yt_title, rule_result,
             llm_result, llm_confidence, recorded_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            post_id, raw_title, filename, yt_title,
            json.dumps(rule_result) if rule_result else None,
            json.dumps(llm_result) if llm_result else None,
            llm_confidence,
            time.time(),
        ),
    )


# ---------------------------------------------------------------------------
# crawl_log table
# ---------------------------------------------------------------------------

def log_crawl(
    conn: sqlite3.Connection,
    *,
    posts_seen: int = 0,
    posts_new: int = 0,
    posts_updated: int = 0,
    clips_new: int = 0,
    album_api_calls: int = 0,
    errors: list[str] | None = None,
) -> int:
    """Insert a crawl_log row and return its id."""
    cur = conn.execute(
        """
        INSERT INTO crawl_log
            (run_at, posts_seen, posts_new, posts_updated,
             clips_new, album_api_calls, errors)
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            time.time(), posts_seen, posts_new, posts_updated,
            clips_new, album_api_calls,
            json.dumps(errors) if errors else None,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]
