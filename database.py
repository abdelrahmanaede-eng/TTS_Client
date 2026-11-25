"""SQLite Database manager for TimeTracker.

Handles input event logs, meeting/break/overtime sessions, persistent settings,
and safe atomic backups to %USERPROFILE%\\Documents\\TimeTracker\\Backups.
Thread-safe with a single write lock and automatic retry with backoff.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from config import get_app_dir, resolve_writable_dir

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Schema version written to the schema_version table. Bump it whenever a
# migration is added; migrations themselves stay idempotent.
SCHEMA_VERSION = 6  # v6: persisted day/change_day columns for PERF-06

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS input_events (
    ts INTEGER PRIMARY KEY,
    app_name TEXT,
    source TEXT DEFAULT 'input',
    day TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER,
    session_type TEXT NOT NULL CHECK(session_type IN ('meeting', 'overtime', 'break')),
    source TEXT NOT NULL CHECK(source IN ('auto', 'tray', 'manual')),
    note TEXT,
    reason TEXT,
    is_pause INTEGER NOT NULL DEFAULT 0,
    day TEXT,
    change_day TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    archived INTEGER NOT NULL DEFAULT 0,
    created_ts INTEGER,
    color TEXT
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    worked_sec INTEGER NOT NULL DEFAULT 0,
    overtime_sec INTEGER NOT NULL DEFAULT 0,
    meeting_sec INTEGER NOT NULL DEFAULT 0,
    break_sec INTEGER NOT NULL DEFAULT 0,
    target_sec INTEGER NOT NULL DEFAULT 0,
    app_secs TEXT NOT NULL DEFAULT '{}',
    project_secs TEXT NOT NULL DEFAULT '{}',
    unclassified_meeting_sec INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER,
    data_version INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_start_end ON sessions (start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions (session_type, end_ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON input_events (ts);
-- PERF-06 day indexes are created in _init_db() after the column migrations,
-- because legacy tables gain the columns later in this same function.
"""


def get_default_db_path() -> Path:
    return get_app_dir() / "data.db"


def get_backups_dir() -> Path:
    """Return the backup directory, with a fallback chain for restricted environments.

    Priority:
    1. %USERPROFILE%\\Documents\\TimeTracker\\Backups  (default, standard user path)
    2. %LOCALAPPDATA%\\TimeTracker\\Backups            (AppData — always writable)
    3. %TEMP%\\TimeTracker\\Backups                    (last resort temp folder)
    """
    candidates = [
        Path.home() / "Documents" / "TimeTracker" / "Backups",
        get_app_dir() / "Backups",
        Path(tempfile.gettempdir()) / "TimeTracker" / "Backups",
    ]
    fallback = Path(tempfile.gettempdir()) / "TimeTracker" / "Backups"
    return resolve_writable_dir(candidates, fallback)


class Database:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path or get_default_db_path())
        self._lock = threading.Lock()
        # PRAGMA journal_mode is persistent in the file, so it only has to be set
        # once per database instead of on every statement (P1-3).
        self._wal_enabled = False
        # Item 3: never migrate an existing database without a copy of it.
        self._backup_before_migration()
        self._init_db()

    def _existing_schema_version(self) -> int:
        """Return the schema version on disk (-1 when there is no database yet)."""
        if not Path(self.db_path).exists():
            return -1
        try:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            try:
                row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            return -1
        if not row or row[0] is None:
            return 0
        return int(row[0])

    def _backup_before_migration(self) -> Path | None:
        """Copy the database before any schema change (item 3).

        The copy uses SQLite's online backup API and lands in
        ``Documents\\TimeTracker\\Backups`` as
        ``pre_migration_v<old>_to_v<new>_<timestamp>.db``. If the copy fails the
        migration is aborted with :class:`RuntimeError`, so an unbacked database
        is never modified.
        """
        old_version = self._existing_schema_version()
        if old_version < 0 or old_version >= SCHEMA_VERSION:
            return None

        target: Path | None = None
        try:
            target_dir = get_backups_dir()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = target_dir / f"pre_migration_v{old_version}_to_v{SCHEMA_VERSION}_{stamp}.db"
            target_dir.mkdir(parents=True, exist_ok=True)
            source = sqlite3.connect(self.db_path, timeout=10.0)
            try:
                dest = sqlite3.connect(str(target))
                try:
                    source.backup(dest)
                finally:
                    dest.close()
            finally:
                source.close()
        except Exception as exc:
            raise RuntimeError(
                f"Could not back up the database before the v{old_version} -> "
                f"v{SCHEMA_VERSION} migration ({exc}). The migration was aborted "
                "and no data was changed."
            ) from exc

        logger.info("Pre-migration backup written to %s", target)
        Database.prune_pre_migration_backups(target_dir)
        return target

    @staticmethod
    def prune_pre_migration_backups(backup_dir: Path, max_age_days: int = 30) -> int:
        """Delete pre-migration backups older than ``max_age_days`` (item 3).

        These copies are deliberately excluded from ``backup_keep_count`` pruning
        so a migration can always be rolled back.
        """
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        try:
            files = list(backup_dir.glob("pre_migration_*.db"))
        except OSError:
            return 0
        for path in files:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                logger.debug("Could not remove old pre-migration backup %s: %s", path, exc)
        return removed

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        if not self._wal_enabled:
            conn.execute("PRAGMA journal_mode = WAL;")
            self._wal_enabled = True
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        return conn

    def close(self) -> None:
        """Kept for API symmetry; connections are opened per operation."""

    def _execute_with_retry(self, func: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a database operation with a lock and 3 retries (200ms backoff)."""
        retries = 3
        backoff_seconds = 0.2

        with self._lock:
            conn = self._get_connection()
            try:
                for attempt in range(1, retries + 1):
                    try:
                        return func(conn)
                    except sqlite3.OperationalError as exc:
                        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                            if attempt < retries:
                                time.sleep(backoff_seconds)
                                continue
                        raise
            finally:
                conn.close()
        raise RuntimeError("database operation failed")  # pragma: no cover

    def _init_db(self) -> None:
        def op(conn: sqlite3.Connection) -> None:
            # Base schema first: executescript() cannot run inside a transaction.
            conn.executescript(SCHEMA)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO schema_version (version) SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_version)")

            # Every migration below runs in one transaction and is idempotent, so
            # an interrupted upgrade can simply be retried (B10).
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. Automatic column migration for input_events
                cursor.execute("PRAGMA table_info(input_events)")
                ev_cols = {row["name"] for row in cursor.fetchall()}
                if "app_name" not in ev_cols:
                    cursor.execute("ALTER TABLE input_events ADD COLUMN app_name TEXT")
                if "source" not in ev_cols:
                    cursor.execute("ALTER TABLE input_events ADD COLUMN source TEXT DEFAULT 'input'")
                if "project_id" not in ev_cols:
                    cursor.execute("ALTER TABLE input_events ADD COLUMN project_id INTEGER")
                if "day" not in ev_cols:
                    cursor.execute("ALTER TABLE input_events ADD COLUMN day TEXT")
                    cursor.execute(
                        "UPDATE input_events SET day = strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') "
                        "WHERE day IS NULL"
                    )

                # 2. Add the new columns first so the constraint migration below
                # can copy them even on very old databases.
                cursor.execute("PRAGMA table_info(sessions)")
                pre_cols = {row["name"] for row in cursor.fetchall()}
                if "note" not in pre_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN note TEXT")
                if "reason" not in pre_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN reason TEXT")
                if "is_pause" not in pre_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN is_pause INTEGER NOT NULL DEFAULT 0")
                if "day" not in pre_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN day TEXT")
                if "change_day" not in pre_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN change_day TEXT")
                # Backfill in one pass; strftime is fine in UPDATE, just not in an index.
                cursor.execute(
                    "UPDATE sessions SET day = strftime('%Y-%m-%d', start_ts, 'unixepoch', 'localtime') "
                    "WHERE day IS NULL"
                )
                cursor.execute(
                    "UPDATE sessions SET change_day = strftime('%Y-%m-%d', COALESCE(end_ts, start_ts), 'unixepoch', 'localtime') "
                    "WHERE change_day IS NULL"
                )

                # 3. Check CHECK constraints on sessions table (old versions
                # only allowed meeting/overtime and auto/tray sources).
                cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'")
                row = cursor.fetchone()
                if row and row["sql"] and ("'break'" not in row["sql"] or "'manual'" not in row["sql"]):
                    logger.info("Migrating sessions table schema...")
                    conn.execute("DROP TABLE IF EXISTS sessions_new")
                    conn.execute("""
                        CREATE TABLE sessions_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            start_ts INTEGER NOT NULL,
                            end_ts INTEGER,
                            session_type TEXT NOT NULL CHECK(session_type IN ('meeting', 'overtime', 'break')),
                            source TEXT NOT NULL CHECK(source IN ('auto', 'tray', 'manual')),
                            note TEXT,
                            reason TEXT,
                            is_pause INTEGER NOT NULL DEFAULT 0,
                            project_id INTEGER,
                            day TEXT,
                            change_day TEXT
                        );
                    """)
                    # Copy existing data safely. project_id only exists on newer
                    # schemas, so select it conditionally to support oldest DBs.
                    old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
                    if "project_id" in old_cols:
                        insert_sql = """
                            INSERT INTO sessions_new (id, start_ts, end_ts, session_type, source, note, reason, is_pause, project_id, day, change_day)
                            SELECT id, start_ts, end_ts, session_type, source,
                                   COALESCE(note, ''), COALESCE(reason, ''), COALESCE(is_pause, 0),
                                   project_id, day, change_day
                            FROM sessions;
                        """
                    else:
                        insert_sql = """
                            INSERT INTO sessions_new (id, start_ts, end_ts, session_type, source, note, reason, is_pause, project_id, day, change_day)
                            SELECT id, start_ts, end_ts, session_type, source,
                                   COALESCE(note, ''), COALESCE(reason, ''), COALESCE(is_pause, 0),
                                   NULL, day, change_day
                            FROM sessions;
                        """
                    conn.execute(insert_sql)
                    conn.execute("DROP TABLE sessions;")
                    conn.execute("ALTER TABLE sessions_new RENAME TO sessions;")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_start_end ON sessions (start_ts, end_ts);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions (session_type, end_ts);")
                    logger.info("Sessions table migration complete.")

                # 4. Column migrations for sessions (project attribution)
                cursor.execute("PRAGMA table_info(sessions)")
                s_cols = {row["name"] for row in cursor.fetchall()}
                if "note" not in s_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN note TEXT")
                if "reason" not in s_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN reason TEXT")
                if "project_id" not in s_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER")
                if "is_pause" not in s_cols:
                    cursor.execute("ALTER TABLE sessions ADD COLUMN is_pause INTEGER NOT NULL DEFAULT 0")

                # 5. Indexes for the attribution columns
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_project ON input_events (project_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON input_events (ts)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions (project_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_summary_date ON daily_summary (date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_events_day ON input_events (day)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_day ON sessions (day)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_change_day ON sessions (change_day)")

                # 5b. Column migrations for daily_summary (v4): unclassified
                # meeting seconds keep D3 auditable after the purge.
                cursor.execute("PRAGMA table_info(daily_summary)")
                ds_cols = {row["name"] for row in cursor.fetchall()}
                if "unclassified_meeting_sec" not in ds_cols:
                    cursor.execute(
                        "ALTER TABLE daily_summary ADD COLUMN "
                        "unclassified_meeting_sec INTEGER NOT NULL DEFAULT 0"
                    )
                if "data_version" not in ds_cols:
                    cursor.execute(
                        "ALTER TABLE daily_summary ADD COLUMN "
                        "data_version INTEGER NOT NULL DEFAULT 0"
                    )

                conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        self._execute_with_retry(op)

    # ---------------------------------------------------------
    # Input Events
    # ---------------------------------------------------------
    def record_input_event(
        self,
        ts: int,
        app_name: str | None = None,
        source: str = "input",
        project_id: int | None = None,
    ) -> bool:
        """Record a single timestamp, active app, source, and project into input_events."""
        def op(conn: sqlite3.Connection) -> bool:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO input_events (ts, app_name, source, project_id, day)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    app_name = COALESCE(excluded.app_name, input_events.app_name),
                    project_id = COALESCE(excluded.project_id, input_events.project_id),
                    source = CASE WHEN excluded.source = 'confirm' THEN 'confirm' ELSE input_events.source END
                """,
                (int(ts), app_name or "Unknown", source, project_id, self._day_from_ts(int(ts))),
            )
            self._bump_day_data_version(conn, self._day_from_ts(ts))
            return cursor.rowcount > 0

        return self._execute_with_retry(op)

    def record_input_events_batch(
        self,
        events: list[int] | list[tuple[int, str]] | list[tuple[int, str, str]] | list[tuple[int, str, str, int | None]],
    ) -> int:
        """Batch insert timestamps, optional app names, sources, and project ids."""
        if not events:
            return 0

        formatted: list[tuple[int, str, str, int | None]] = []
        for item in events:
            if isinstance(item, tuple):
                project_id = None
                if len(item) >= 4 and item[3] is not None:
                    try:
                        project_id = int(item[3])
                    except (TypeError, ValueError):
                        project_id = None
                if len(item) == 2:
                    formatted.append((int(item[0]), str(item[1]), "input", None))
                elif len(item) >= 3:
                    formatted.append((int(item[0]), str(item[1]), str(item[2]), project_id))
                else:
                    formatted.append((int(item[0]), "Unknown", "input", None))
            else:
                formatted.append((int(item), "Unknown", "input", None))

        # PERF-06: precompute the local day once per row for the indexed column.
        formatted = [(*item, self._day_from_ts(item[0])) for item in formatted]

        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO input_events (ts, app_name, source, project_id, day)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    app_name = COALESCE(excluded.app_name, input_events.app_name),
                    project_id = COALESCE(excluded.project_id, input_events.project_id),
                    source = CASE WHEN excluded.source = 'confirm' THEN 'confirm' ELSE input_events.source END
                """,
                formatted,
            )
            for _day in {self._day_from_ts(e[0]) for e in formatted}:
                self._bump_day_data_version(conn, _day)
            return cursor.rowcount

        return self._execute_with_retry(op)

    def get_input_events_between(self, start_ts: int, end_ts: int) -> list[int]:
        """Return sorted timestamps in the half-open range [start_ts, end_ts)."""
        def op(conn: sqlite3.Connection) -> list[int]:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ts FROM input_events WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                (int(start_ts), int(end_ts)),
            )
            return [row["ts"] for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    def get_input_events_with_apps(self, start_ts: int, end_ts: int) -> list[tuple[int, str]]:
        """Return sorted list of (timestamp, app_name) tuples between start_ts and end_ts."""
        def op(conn: sqlite3.Connection) -> list[tuple[int, str]]:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ts, COALESCE(app_name, 'Unknown') AS app
                FROM input_events
                WHERE ts >= ? AND ts < ?
                ORDER BY ts ASC
                """,
                (int(start_ts), int(end_ts)),
            )
            return [(int(row["ts"]), str(row["app"])) for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    def get_input_events_with_sources(self, start_ts: int, end_ts: int) -> list[tuple[int, str, str]]:
        """Return sorted list of (timestamp, app_name, source) tuples between start_ts and end_ts."""
        def op(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ts, COALESCE(app_name, 'Unknown') AS app, COALESCE(source, 'input') AS src
                FROM input_events
                WHERE ts >= ? AND ts < ?
                ORDER BY ts ASC
                """,
                (int(start_ts), int(end_ts)),
            )
            return [(int(row["ts"]), str(row["app"]), str(row["src"])) for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    def get_input_events_with_details(
        self, start_ts: int, end_ts: int
    ) -> list[tuple[int, str, str, int | None]]:
        """Return ``(timestamp, app_name, source, project_id)`` tuples in range."""
        def op(conn: sqlite3.Connection) -> list[tuple[int, str, str, int | None]]:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT ts, COALESCE(app_name, 'Unknown') AS app,
                       COALESCE(source, 'input') AS src,
                       project_id
                FROM input_events
                WHERE ts >= ? AND ts < ?
                ORDER BY ts ASC
                """,
                (int(start_ts), int(end_ts)),
            )
            rows = []
            for row in cursor.fetchall():
                pid = row["project_id"]
                rows.append((int(row["ts"]), str(row["app"]), str(row["src"]), int(pid) if pid is not None else None))
            return rows

        return self._execute_with_retry(op)

    # ---------------------------------------------------------
    # Sessions
    # ---------------------------------------------------------
    def start_session(
        self,
        session_type: str,
        source: str,
        start_ts: int,
        note: str = "",
        reason: str = "",
        project_id: int | None = None,
        is_pause: bool = False,
    ) -> int:
        """Start a new session (meeting, overtime, or break)."""
        if session_type not in ("meeting", "overtime", "break"):
            raise ValueError(f"Invalid session_type: {session_type}")
        if source not in ("auto", "tray", "manual"):
            raise ValueError(f"Invalid source: {source}")

        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM sessions WHERE session_type = ? AND end_ts IS NULL LIMIT 1",
                (session_type,),
            )
            row = cursor.fetchone()
            if row:
                return int(row["id"])

            cursor.execute(
                """
                INSERT INTO sessions (start_ts, end_ts, session_type, source, note, reason, project_id, is_pause, day, change_day)
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(start_ts), session_type, source, note or "", reason or "", project_id,
                    1 if is_pause else 0, self._day_from_ts(start_ts), self._day_from_ts(start_ts),
                ),
            )
            self._bump_day_data_version(conn, self._day_from_ts(start_ts))
            return int(cursor.lastrowid)

        return self._execute_with_retry(op)

    def add_past_session(
        self,
        session_type: str,
        start_ts: int,
        end_ts: int,
        source: str = "manual",
        note: str = "",
        reason: str = "",
        project_id: int | None = None,
    ) -> int:
        """Add a completed past session retroactively."""
        if session_type not in ("meeting", "overtime", "break"):
            raise ValueError(f"Invalid session_type: {session_type}")
        if end_ts <= start_ts:
            raise ValueError("End time must be after start time")

        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO sessions (start_ts, end_ts, session_type, source, note, reason, project_id, day, change_day)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(start_ts), int(end_ts), session_type, source, note or "", reason or "",
                    project_id, self._day_from_ts(start_ts), self._day_from_ts(end_ts),
                ),
            )
            self._bump_day_data_version(conn, self._day_from_ts(start_ts))
            self._bump_day_data_version(conn, self._day_from_ts(end_ts))
            return int(cursor.lastrowid)

        return self._execute_with_retry(op)

    def update_session_note_or_reason(
        self,
        session_id: int,
        note: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Update note or reason for a given session."""
        def op(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            if note is not None and reason is not None:
                cursor.execute("UPDATE sessions SET note = ?, reason = ? WHERE id = ?", (note, reason, session_id))
            elif note is not None:
                cursor.execute("UPDATE sessions SET note = ? WHERE id = ?", (note, session_id))
            elif reason is not None:
                cursor.execute("UPDATE sessions SET reason = ? WHERE id = ?", (reason, session_id))
            if note is not None or reason is not None:
                cursor.execute("SELECT start_ts FROM sessions WHERE id = ?", (session_id,))
                _row = cursor.fetchone()
                _ts = int(_row["start_ts"]) if _row and _row["start_ts"] is not None else 0
                self._bump_day_data_version(conn, self._day_from_ts(_ts))

        self._execute_with_retry(op)

    def end_session(self, session_type: str, end_ts: int, note: str | None = None) -> list[dict[str, Any]]:
        """Close any open sessions of session_type at end_ts."""
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, start_ts, session_type, source, note, reason, project_id, is_pause FROM sessions WHERE session_type = ? AND end_ts IS NULL",
                (session_type,),
            )
            rows = cursor.fetchall()
            closed = []
            for row in rows:
                sid = int(row["id"])
                sts = int(row["start_ts"])
                actual_end = max(sts, int(end_ts))
                new_note = note if note is not None else row["note"]
                cursor.execute(
                    "UPDATE sessions SET end_ts = ?, note = ?, change_day = ? WHERE id = ?",
                    (actual_end, new_note or "", self._day_from_ts(actual_end), sid),
                )
                closed.append({
                    "id": sid,
                    "start_ts": sts,
                    "end_ts": actual_end,
                    "duration": actual_end - sts,
                    "session_type": row["session_type"],
                    "source": row["source"],
                    "note": new_note,
                    "reason": row["reason"],
                    "project_id": row["project_id"],
                })
            if closed:
                for _s in closed:
                    self._bump_day_data_version(conn, self._day_from_ts(int(_s.get("start_ts") or 0)))
            return closed

        return self._execute_with_retry(op)

    def get_open_session(self, session_type: str) -> dict[str, Any] | None:
        """Return the current open session of session_type, or None."""
        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, start_ts, end_ts, session_type, source, note, reason, project_id, is_pause
                FROM sessions
                WHERE session_type = ? AND end_ts IS NULL
                ORDER BY start_ts DESC LIMIT 1
                """,
                (session_type,),
            )
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

        return self._execute_with_retry(op)

    def get_recorded_dates(self) -> list[str]:
        """Return a sorted list of unique YYYY-MM-DD dates that have events or sessions."""
        def op(conn: sqlite3.Connection) -> list[str]:
            cursor = conn.cursor()
            # PERF-06: use the precomputed, indexed day columns instead of
            # strftime() over the whole events/sessions tables on every poll.
            cursor.execute("""
                SELECT DISTINCT day AS d FROM input_events WHERE day IS NOT NULL
                UNION
                SELECT DISTINCT day AS d FROM sessions WHERE day IS NOT NULL
                UNION
                SELECT date AS d FROM daily_summary
                ORDER BY d DESC
            """)
            return [row["d"] for row in cursor.fetchall() if row["d"]]

        return self._execute_with_retry(op)

    def get_all_open_sessions(self) -> list[dict[str, Any]]:
        """Return all sessions that have not ended yet."""
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, start_ts, end_ts, session_type, source, note, reason, project_id, is_pause
                FROM sessions
                WHERE end_ts IS NULL
                ORDER BY start_ts ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    def close_all_open_sessions(self, end_ts: int, auto_end_ts: int | None = None) -> int:
        """Close any remaining open sessions on quit/shutdown.

        ``auto_end_ts`` (P1-10) is used for ``source='auto'`` sessions so an
        abandoned overtime/meeting session does not inflate to the quit time;
        tray and manual sessions keep closing at ``end_ts``.
        """
        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("SELECT id, start_ts, source FROM sessions WHERE end_ts IS NULL")
            rows = cursor.fetchall()
            for row in rows:
                sid = int(row["id"])
                sts = int(row["start_ts"])
                target = int(end_ts)
                if auto_end_ts is not None and str(row["source"]) == "auto":
                    target = int(auto_end_ts)
                actual_end = max(sts, target)
                cursor.execute(
                    "UPDATE sessions SET end_ts = ?, change_day = ? WHERE id = ?",
                    (actual_end, self._day_from_ts(actual_end), sid),
                )
            if rows:
                for _r in rows:
                    self._bump_day_data_version(conn, self._day_from_ts(int(_r["start_ts"])))
            return len(rows)

        return self._execute_with_retry(op)

    def cleanup_orphan_sessions(self, reference_ts: int | None = None) -> int:
        """Close any orphan sessions left open from an unexpected shutdown/crash.

        Uses reference_ts if provided; otherwise determines the most recent activity timestamp
        from the last recorded heartbeat in settings or the latest event in input_events.
        """
        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("SELECT id, start_ts, session_type, note FROM sessions WHERE end_ts IS NULL")
            orphans = cursor.fetchall()
            if not orphans:
                return 0

            fallback_ts = reference_ts
            if fallback_ts is None:
                cursor.execute("SELECT value FROM settings WHERE key = 'last_heartbeat_ts'")
                hb_row = cursor.fetchone()
                hb_ts = int(hb_row["value"]) if hb_row and hb_row["value"] and hb_row["value"].isdigit() else 0

                cursor.execute("SELECT MAX(ts) AS max_ts FROM input_events")
                ev_row = cursor.fetchone()
                ev_ts = int(ev_row["max_ts"]) if ev_row and ev_row["max_ts"] is not None else 0

                fallback_ts = max(hb_ts, ev_ts)

            closed_count = 0
            for row in orphans:
                sid = int(row["id"])
                sts = int(row["start_ts"])
                actual_end = max(sts, int(fallback_ts) if fallback_ts else sts)
                existing_note = row["note"] or ""
                recovery_note = f"{existing_note} [Recovered after unexpected shutdown]".strip()
                cursor.execute(
                    "UPDATE sessions SET end_ts = ?, note = ?, change_day = ? WHERE id = ?",
                    (actual_end, recovery_note, self._day_from_ts(actual_end), sid),
                )
                closed_count += 1
            if closed_count:
                for _row in orphans:
                    self._bump_day_data_version(conn, self._day_from_ts(int(_row["start_ts"])))
            return closed_count

        return self._execute_with_retry(op)

    def record_heartbeat(self, ts: int) -> None:
        """Record current timestamp as heartbeat in settings."""
        self.set_setting("last_heartbeat_ts", str(int(ts)))

    def cleanup_old_events(self, retention_days: int = 90) -> int:
        """Delete input events older than retention_days (defaults to 90 days).

        This is the raw purge primitive.  Normal operation goes through
        :func:`retention.purge_old_data`, which aggregates the affected days into
        ``daily_summary`` first so long-term reports keep their totals.
        """
        cutoff_ts = int(time.time()) - (retention_days * 86400)
        return self.delete_input_events_before(cutoff_ts)

    def delete_input_events_before(self, cutoff_ts: int) -> int:
        """Delete input events strictly older than ``cutoff_ts``. Returns rows removed."""
        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM input_events WHERE ts < ?", (int(cutoff_ts),))
            return max(0, cursor.rowcount)

        return self._execute_with_retry(op)

    def get_sessions_between(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        """Return all sessions overlapping [start_ts, end_ts]."""
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, start_ts, end_ts, session_type, source, note, reason, project_id, is_pause
                FROM sessions
                WHERE start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)
                ORDER BY start_ts ASC
                """,
                (int(end_ts), int(start_ts)),
            )
            return [dict(row) for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    # ---------------------------------------------------------
    # Projects
    # ---------------------------------------------------------
    def add_project(self, name: str, color: str | None = None, created_ts: int | None = None) -> int:
        """Create a project. Raises ValueError if the name is empty or duplicated."""
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Project name must not be empty")

        def op(conn: sqlite3.Connection) -> int:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO projects (name, archived, created_ts, color) VALUES (?, 0, ?, ?)",
                    (clean_name, int(created_ts or time.time()), color),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Project already exists: {clean_name}") from exc
            return int(cursor.lastrowid)

        return self._execute_with_retry(op)

    def rename_project(self, project_id: int, new_name: str) -> None:
        clean_name = (new_name or "").strip()
        if not clean_name:
            raise ValueError("Project name must not be empty")

        def op(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            try:
                cursor.execute("UPDATE projects SET name = ? WHERE id = ?", (clean_name, int(project_id)))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"Project already exists: {clean_name}") from exc

        self._execute_with_retry(op)

    def set_project_archived(self, project_id: int, archived: bool = True) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE projects SET archived = ? WHERE id = ?",
                (1 if archived else 0, int(project_id)),
            )

        self._execute_with_retry(op)

    def get_project(self, project_id: int | None) -> dict[str, Any] | None:
        if project_id is None:
            return None

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, archived, created_ts, color FROM projects WHERE id = ?", (int(project_id),))
            row = cursor.fetchone()
            return dict(row) if row else None

        return self._execute_with_retry(op)

    def list_projects(self, include_archived: bool = False) -> list[dict[str, Any]]:
        """Return projects ordered by name (archived rows are optional)."""
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cursor = conn.cursor()
            if include_archived:
                cursor.execute("SELECT id, name, archived, created_ts, color FROM projects ORDER BY name COLLATE NOCASE ASC")
            else:
                cursor.execute(
                    "SELECT id, name, archived, created_ts, color FROM projects WHERE archived = 0 ORDER BY name COLLATE NOCASE ASC"
                )
            return [dict(row) for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    # ---------------------------------------------------------
    # Daily Summary (long-term retention without losing reports)
    # ---------------------------------------------------------
    def upsert_daily_summary(
        self,
        summary_date: str | date,
        worked_sec: int,
        overtime_sec: int,
        meeting_sec: int,
        break_sec: int,
        target_sec: int,
        app_secs: dict[str, int] | None = None,
        project_secs: dict[str, int] | None = None,
        data_version: int | None = None,
    ) -> None:
        """Insert or replace the aggregated summary for a single day."""
        if data_version is None:
            _d = summary_date.isoformat() if hasattr(summary_date, "isoformat") else str(summary_date)
            data_version = self.get_day_data_version(_d)
        date_str = summary_date.isoformat() if isinstance(summary_date, date) else str(summary_date)
        payload = {
            "date": date_str,
            "worked_sec": int(worked_sec),
            "overtime_sec": int(overtime_sec),
            "meeting_sec": int(meeting_sec),
            "break_sec": int(break_sec),
            "target_sec": int(target_sec),
            "app_secs": json.dumps(app_secs or {}, ensure_ascii=False, sort_keys=True),
            "project_secs": json.dumps(project_secs or {}, ensure_ascii=False, sort_keys=True),
            "updated_ts": int(time.time()),
            "data_version": int(data_version),
        }

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO daily_summary
                    (date, worked_sec, overtime_sec, meeting_sec, break_sec, target_sec, app_secs, project_secs, updated_ts, data_version)
                VALUES (:date, :worked_sec, :overtime_sec, :meeting_sec, :break_sec, :target_sec, :app_secs, :project_secs, :updated_ts, :data_version)
                ON CONFLICT(date) DO UPDATE SET
                    worked_sec = excluded.worked_sec,
                    overtime_sec = excluded.overtime_sec,
                    meeting_sec = excluded.meeting_sec,
                    break_sec = excluded.break_sec,
                    target_sec = excluded.target_sec,
                    app_secs = excluded.app_secs,
                    project_secs = excluded.project_secs,
                    updated_ts = excluded.updated_ts,
                    data_version = excluded.data_version
                """,
                payload,
            )

        self._execute_with_retry(op)

    @staticmethod
    def _decode_summary_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("app_secs", "project_secs"):
            raw = result.get(key)
            if isinstance(raw, str) and raw:
                try:
                    decoded = json.loads(raw)
                    result[key] = decoded if isinstance(decoded, dict) else {}
                except json.JSONDecodeError:
                    result[key] = {}
            else:
                result[key] = {}
        return result

    def set_summary_unclassified(self, summary_date: str | date, seconds: int) -> None:
        """Store the unclassified meeting seconds of an aggregated day."""
        date_str = summary_date.isoformat() if isinstance(summary_date, date) else str(summary_date)

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE daily_summary SET unclassified_meeting_sec = ? WHERE date = ?",
                (int(seconds), date_str),
            )

        self._execute_with_retry(op)

    def get_last_data_change_by_day(self) -> dict[str, int]:
        """Newest data timestamp per local date, across events and sessions.

        Used to decide whether a ``daily_summary`` row is stale (item 1).
        """
        def op(conn: sqlite3.Connection) -> dict[str, int]:
            cursor = conn.cursor()
            result: dict[str, int] = {}
            # PERF-06: precomputed indexed columns replace the full strftime scans.
            cursor.execute(
                """
                SELECT day AS d, MAX(ts) AS last_ts
                FROM input_events WHERE day IS NOT NULL GROUP BY day
                """
            )
            for row in cursor.fetchall():
                if row["d"]:
                    result[str(row["d"])] = int(row["last_ts"] or 0)
            cursor.execute(
                """
                SELECT change_day AS d, MAX(COALESCE(end_ts, start_ts)) AS last_ts
                FROM sessions WHERE change_day IS NOT NULL GROUP BY change_day
                """
            )
            for row in cursor.fetchall():
                if not row["d"]:
                    continue
                key = str(row["d"])
                value = int(row["last_ts"] or 0)
                result[key] = max(result.get(key, 0), value)
            return result

        return self._execute_with_retry(op)

    @staticmethod
    def _day_from_ts(ts: int) -> str:
        from datetime import datetime

        try:
            return datetime.fromtimestamp(int(ts)).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    def get_day_data_version(self, day_str: str) -> int:
        """Return the per-day data-version counter for ``day_str`` (increments on mutation)."""
        if not day_str:
            return 0
        val = self.get_setting(f"dayver:{day_str}")
        try:
            return int(val) if val is not None else 0
        except (TypeError, ValueError):
            return 0

    def _bump_day_data_version(self, conn: sqlite3.Connection, day_str: str) -> None:
        """Increment the per-day data-version counter within an existing connection."""
        if not day_str:
            return
        key = f"dayver:{day_str}"
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (?, '1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)",
            (key,),
        )

    def get_daily_summary(self, summary_date: str | date) -> dict[str, Any] | None:
        date_str = summary_date.isoformat() if isinstance(summary_date, date) else str(summary_date)

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM daily_summary WHERE date = ?", (date_str,))
            row = cursor.fetchone()
            return self._decode_summary_row(row) if row else None

        return self._execute_with_retry(op)

    def get_daily_summaries(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """Return summaries for [start_date, end_date] ordered by date."""
        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM daily_summary WHERE date >= ? AND date <= ? ORDER BY date ASC",
                (start_date.isoformat(), end_date.isoformat()),
            )
            return [self._decode_summary_row(row) for row in cursor.fetchall()]

        return self._execute_with_retry(op)

    # ---------------------------------------------------------
    # Backup Management
    # ---------------------------------------------------------
    def backup_database(self, backup_dir: Path | None = None, keep: int | None = None) -> Path:
        """Create a safe, atomic SQLite backup of data.db to the Backups folder."""
        target_dir = backup_dir or get_backups_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        backup_filename = f"TimeTracker_backup_{date.today().isoformat()}.db"
        target_path = target_dir / backup_filename

        with self._lock:
            source_conn = self._get_connection()
            try:
                dest_conn = sqlite3.connect(str(target_path))
                try:
                    source_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
            finally:
                source_conn.close()

        logger.info("Database backup created successfully at %s", target_path)
        if keep is not None:
            removed = self.prune_backups(target_dir, keep)
            if removed:
                logger.info("Pruned %d old database backup(s), keeping %d.", removed, keep)
        return target_path

    @staticmethod
    def prune_backups(backup_dir: Path, keep: int) -> int:
        """Delete the oldest backups so at most ``keep`` remain (P1-11).

        Backup names embed an ISO date, so lexical order is chronological.
        """
        try:
            files = sorted(backup_dir.glob("TimeTracker_backup_*.db"), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("Could not list backups in %s: %s", backup_dir, exc)
            return 0
        keep = max(1, int(keep))
        removed = 0
        for path in files[:-keep]:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Could not remove old backup %s: %s", path, exc)
        return removed

    # ---------------------------------------------------------
    # Settings Key/Value
    # ---------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        def op(conn: sqlite3.Connection) -> str | None:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return str(row["value"])
            return default

        return self._execute_with_retry(op)

    def set_setting(self, key: str, value: str) -> None:
        def op(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

        self._execute_with_retry(op)
