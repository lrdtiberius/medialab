from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .parser import ParsedMedia


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    stable_since TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    processed_at TEXT,

    status TEXT NOT NULL DEFAULT 'waiting',
    message TEXT NOT NULL DEFAULT '',
    source_hint TEXT,

    media_type TEXT NOT NULL,
    library TEXT,
    parsed_title TEXT NOT NULL,
    parsed_year INTEGER,
    season INTEGER,
    episode INTEGER,
    episode_end INTEGER,
    technical_tags TEXT NOT NULL DEFAULT '[]',
    technical_info TEXT NOT NULL DEFAULT '{}',

    tmdb_id INTEGER,
    tmdb_title TEXT,
    tmdb_year INTEGER,
    match_score REAL,
    target_path TEXT,
    search_query TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at DESC);

CREATE TABLE IF NOT EXISTS media_probe_cache (
    source_path TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    probe_json TEXT NOT NULL DEFAULT '{}',
    probed_at TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_media_probe_cache_probed_at
    ON media_probe_cache(probed_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        name: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            # Migration for databases created by 0.2.x.
            self._ensure_column(
                connection,
                "jobs",
                "technical_info",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            job_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "media_info" in job_columns:
                connection.execute(
                    """
                    UPDATE jobs
                    SET technical_info = media_info
                    WHERE (technical_info IS NULL OR technical_info = '{}')
                      AND media_info IS NOT NULL
                      AND media_info != '{}'
                    """
                )
            connection.execute(
                """
                UPDATE jobs
                SET library = 'series',
                    source_hint = CASE
                        WHEN source_hint = 'anime' THEN 'series'
                        ELSE source_hint
                    END
                WHERE media_type = 'tv'
                  AND (library = 'anime' OR source_hint = 'anime')
                """
            )

    @staticmethod
    def _decode_json(value: Any, fallback: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value or "")
        except (json.JSONDecodeError, TypeError):
            return fallback

    @classmethod
    def _row_to_dict(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["technical_tags"] = cls._decode_json(
            result.get("technical_tags"), []
        )
        result["technical_info"] = cls._decode_json(result.get("technical_info"), {})
        return result

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row)

    def get_job_by_path(self, source_path: Path | str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE source_path = ?", (str(source_path),)
            ).fetchone()
        return self._row_to_dict(row)

    def upsert_discovered(
        self,
        source_path: Path,
        size_bytes: int,
        mtime: float,
        parsed: ParsedMedia,
        source_hint: str | None,
    ) -> dict[str, Any]:
        now = utcnow()
        encoded_tags = json.dumps(parsed.technical_tags, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE source_path = ?", (str(source_path),)
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO jobs (
                        source_path, source_name, size_bytes, mtime, stable_since,
                        discovered_at, updated_at, status, source_hint, media_type,
                        library, parsed_title, parsed_year, season, episode,
                        episode_end, technical_tags, technical_info, search_query
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                    """,
                    (
                        str(source_path),
                        source_path.name,
                        size_bytes,
                        mtime,
                        now,
                        now,
                        now,
                        source_hint,
                        parsed.media_type,
                        source_hint if source_hint in {"movies", "series", "anime"} else None,
                        parsed.title,
                        parsed.year,
                        parsed.season,
                        parsed.episode,
                        parsed.episode_end,
                        encoded_tags,
                        parsed.title,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                assert row is not None
                return self._row_to_dict(row) or {}

            changed = existing["size_bytes"] != size_bytes or existing["mtime"] != mtime
            source_reappeared = existing["status"] == "processed"
            if changed or source_reappeared:
                connection.execute(
                    """
                    UPDATE jobs SET
                        source_name = ?, size_bytes = ?, mtime = ?, stable_since = ?,
                        updated_at = ?, processed_at = NULL, status = 'waiting', message = '',
                        source_hint = ?, media_type = ?, library = ?, parsed_title = ?,
                        parsed_year = ?, season = ?, episode = ?, episode_end = ?,
                        technical_tags = ?, technical_info = '{}', tmdb_id = NULL, tmdb_title = NULL,
                        tmdb_year = NULL, match_score = NULL, target_path = NULL,
                        search_query = ?
                    WHERE id = ?
                    """,
                    (
                        source_path.name,
                        size_bytes,
                        mtime,
                        now,
                        now,
                        source_hint,
                        parsed.media_type,
                        source_hint if source_hint in {"movies", "series", "anime"} else None,
                        parsed.title,
                        parsed.year,
                        parsed.season,
                        parsed.episode,
                        parsed.episode_end,
                        encoded_tags,
                        parsed.title,
                        existing["id"],
                    ),
                )
            else:
                connection.execute(
                    "UPDATE jobs SET updated_at = ? WHERE id = ?", (now, existing["id"])
                )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (existing["id"],)
            ).fetchone()
        assert row is not None
        return self._row_to_dict(row) or {}

    def mark_ready(self, job_id: int) -> None:
        self.update_job(job_id, status="pending", message="Datei ist vollständig und bereit.")

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {
            "status",
            "message",
            "source_hint",
            "media_type",
            "library",
            "parsed_title",
            "parsed_year",
            "season",
            "episode",
            "episode_end",
            "technical_tags",
            "technical_info",
            "tmdb_id",
            "tmdb_title",
            "tmdb_year",
            "match_score",
            "target_path",
            "search_query",
            "processed_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown job fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        for name, fallback in (("technical_tags", []), ("technical_info", {})):
            if name in fields and not isinstance(fields[name], str):
                fields[name] = json.dumps(fields[name] if fields[name] is not None else fallback, ensure_ascii=False)
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = list(fields.values()) + [job_id]
        with self._connect() as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)

    def reset_job(self, job_id: int) -> None:
        self.update_job(
            job_id,
            status="pending",
            message="Erneuter Erkennungsversuch angefordert.",
            tmdb_id=None,
            tmdb_title=None,
            tmdb_year=None,
            match_score=None,
            target_path=None,
            processed_at=None,
        )

    def list_jobs(
        self,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if statuses:
            status_list = list(statuses)
            placeholders = ",".join("?" for _ in status_list)
            where = f"WHERE status IN ({placeholders})"
            params.extend(status_list)
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs {where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_dict(row) or {} for row in rows]

    def counts(self) -> dict[str, int]:
        defaults = {
            "waiting": 0,
            "pending": 0,
            "matched": 0,
            "unresolved": 0,
            "processing": 0,
            "processed": 0,
            "error": 0,
            "ignored": 0,
        }
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        for row in rows:
            defaults[row["status"]] = row["count"]
        return defaults

    def remove_missing_sources(self, observed_paths: Iterable[Path | str]) -> int:
        """Remove stale inbox rows after files were renamed or deleted manually.

        Processed rows are kept as history. Rows currently being processed are
        protected against a race with the scanner.
        """

        observed = {str(path) for path in observed_paths}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_path FROM jobs
                WHERE status NOT IN ('processed', 'processing')
                """
            ).fetchall()
            stale = [(row["id"],) for row in rows if row["source_path"] not in observed]
            if stale:
                connection.executemany("DELETE FROM jobs WHERE id = ?", stale)
        return len(stale)

    def prune_missing_sources(self, inbox_root: Path | str) -> int:
        """Remove non-processed jobs whose source file no longer exists.

        This is used when the dashboard is opened before the next scheduled
        scan. It makes externally renamed files disappear from the overview
        immediately while preserving processed history.
        """

        root = Path(inbox_root).resolve()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_path FROM jobs
                WHERE status NOT IN ('processed', 'processing')
                """
            ).fetchall()
            stale: list[tuple[int]] = []
            for row in rows:
                path = Path(row["source_path"])
                try:
                    path.resolve().relative_to(root)
                except (OSError, ValueError):
                    continue
                try:
                    exists = path.is_file()
                except OSError:
                    exists = False
                if not exists:
                    stale.append((int(row["id"]),))
            if stale:
                connection.executemany("DELETE FROM jobs WHERE id = ?", stale)
        return len(stale)

    def trim_history(self, history_limit: int) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM jobs
                WHERE status IN ('processed', 'ignored')
                ORDER BY updated_at DESC
                LIMIT -1 OFFSET ?
                """,
                (history_limit,),
            ).fetchall()
            if rows:
                connection.executemany(
                    "DELETE FROM jobs WHERE id = ?", [(row["id"],) for row in rows]
                )

    # ffprobe cache ---------------------------------------------------------

    def get_media_probe(
        self, source_path: Path | str, size_bytes: int, mtime: float
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_probe_cache WHERE source_path = ?",
                (str(source_path),),
            ).fetchone()
        if row is None:
            return None
        if row["size_bytes"] != size_bytes or abs(float(row["mtime"]) - float(mtime)) > 0.0001:
            return None
        payload = self._decode_json(row["probe_json"], {})
        return payload if isinstance(payload, dict) and payload else None

    def store_media_probe(
        self,
        source_path: Path | str,
        size_bytes: int,
        mtime: float,
        info: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_probe_cache (
                    source_path, size_bytes, mtime, probe_json, probed_at, error
                ) VALUES (?, ?, ?, ?, ?, '')
                ON CONFLICT(source_path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    mtime = excluded.mtime,
                    probe_json = excluded.probe_json,
                    probed_at = excluded.probed_at,
                    error = ''
                """,
                (
                    str(source_path),
                    size_bytes,
                    mtime,
                    json.dumps(info, ensure_ascii=False),
                    utcnow(),
                ),
            )

    def store_media_probe_error(
        self,
        source_path: Path | str,
        size_bytes: int,
        mtime: float,
        error: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_probe_cache (
                    source_path, size_bytes, mtime, probe_json, probed_at, error
                ) VALUES (?, ?, ?, '{}', ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    mtime = excluded.mtime,
                    probe_json = '{}',
                    probed_at = excluded.probed_at,
                    error = excluded.error
                """,
                (str(source_path), size_bytes, mtime, utcnow(), error[:1000]),
            )


    def list_media_probes(
        self, *, prefix: Path | str | None = None, limit: int = 100000
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if prefix is not None:
            normalized = str(Path(prefix).resolve()).rstrip("/")
            where = "WHERE source_path = ? OR source_path LIKE ?"
            params.extend((normalized, normalized + "/%"))
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT source_path, size_bytes, mtime, probe_json, probed_at, error
                FROM media_probe_cache
                {where}
                ORDER BY source_path
                LIMIT ?
                """,
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = self._decode_json(row["probe_json"], {})
            result.append(
                {
                    "file_path": row["source_path"],
                    "size_bytes": int(row["size_bytes"]),
                    "mtime": float(row["mtime"]),
                    "payload": payload if isinstance(payload, dict) else {},
                    "probed_at": row["probed_at"],
                    "error": row["error"] or "",
                }
            )
        return result

    def list_media_probes_under(
        self, folder: Path | str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        return [
            row["payload"]
            for row in self.list_media_probes(prefix=folder, limit=limit)
            if not row["error"] and row["payload"]
        ]

    def relocate_media_probe(self, source: Path | str, target: Path | str) -> None:
        source_str = str(source)
        target_str = str(target)
        try:
            stat = Path(target).stat()
        except OSError:
            stat = None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM media_probe_cache WHERE source_path = ?", (source_str,)
            ).fetchone()
            if row is None:
                return
            connection.execute("DELETE FROM media_probe_cache WHERE source_path = ?", (target_str,))
            connection.execute(
                """
                UPDATE media_probe_cache
                SET source_path = ?, size_bytes = ?, mtime = ?
                WHERE source_path = ?
                """,
                (
                    target_str,
                    stat.st_size if stat else row["size_bytes"],
                    stat.st_mtime if stat else row["mtime"],
                    source_str,
                ),
            )

    def list_media_probe_errors(self, *, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_path, size_bytes, mtime, probed_at, error
                FROM media_probe_cache
                WHERE error != ''
                ORDER BY probed_at DESC, source_path
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "file_path": row["source_path"],
                "size_bytes": int(row["size_bytes"]),
                "mtime": float(row["mtime"]),
                "probed_at": row["probed_at"],
                "error": row["error"] or "",
            }
            for row in rows
        ]

    def media_probe_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN probe_json != '{}' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) AS errors,
                    COUNT(*) AS total
                FROM media_probe_cache
                """
            ).fetchone()
        success = int((row and row["success"]) or 0)
        return {
            "success": success,
            "successful": success,
            "errors": int((row and row["errors"]) or 0),
            "total": int((row and row["total"]) or 0),
        }
