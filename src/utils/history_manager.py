from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.app.models import LessonDownloadReport


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HistoryManager:
    """Persists download history to a SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS download_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                platform_name TEXT NOT NULL,
                status TEXT DEFAULT 'in_progress'
            );
            CREATE TABLE IF NOT EXISTS download_lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES download_sessions(id),
                course_id TEXT NOT NULL,
                course_name TEXT NOT NULL,
                module_name TEXT NOT NULL,
                lesson_name TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                started_at TEXT,
                completed_at TEXT,
                lesson_path TEXT,
                video_count INTEGER DEFAULT 0,
                video_success INTEGER DEFAULT 0,
                attachment_count INTEGER DEFAULT 0,
                attachment_success INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS download_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lesson_id INTEGER NOT NULL REFERENCES download_lessons(id),
                item_type TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                error_type TEXT,
                error_message TEXT
            );
        """)
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns that may be missing from older databases."""
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(download_lessons)").fetchall()
        }
        migrations: list[str] = []
        if "started_at" not in cols:
            migrations.append("ALTER TABLE download_lessons ADD COLUMN started_at TEXT")
        if "completed_at" not in cols:
            migrations.append("ALTER TABLE download_lessons ADD COLUMN completed_at TEXT")
        if "lesson_path" not in cols:
            migrations.append("ALTER TABLE download_lessons ADD COLUMN lesson_path TEXT")
        # rename old 'recorded_at' values into completed_at
        if "recorded_at" in cols and "completed_at" not in cols:
            migrations.append(
                "UPDATE download_lessons SET completed_at = recorded_at WHERE completed_at IS NULL"
            )
        for stmt in migrations:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        if migrations:
            self._conn.commit()

    def start_session(self, platform_name: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO download_sessions (started_at, platform_name) VALUES (?, ?)",
            (_now(), platform_name),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def record_lesson(
        self,
        session_id: int,
        course_id: str,
        course_name: str,
        module_name: str,
        lesson_name: str,
        report: LessonDownloadReport,
        started_at: str | None = None,
        lesson_path: str | None = None,
    ) -> None:
        video_count = len(report.videos)
        video_success = sum(1 for v in report.videos if v.status == "success")
        attachment_count = len(report.attachments)
        attachment_success = sum(1 for a in report.attachments if a.status == "success")

        cursor = self._conn.execute(
            """INSERT INTO download_lessons
               (session_id, course_id, course_name, module_name, lesson_name,
                status, error_message, started_at, completed_at, lesson_path,
                video_count, video_success, attachment_count, attachment_success)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                str(course_id),
                course_name,
                module_name,
                lesson_name,
                report.status,
                report.error_message,
                started_at or _now(),
                _now(),
                lesson_path,
                video_count,
                video_success,
                attachment_count,
                attachment_success,
            ),
        )
        lesson_id = cursor.lastrowid

        item_rows = []
        for v in report.videos:
            item_rows.append((lesson_id, "video", v.name, v.status, v.error_type, v.error_message))
        for a in report.attachments:
            item_rows.append((lesson_id, "attachment", a.name, a.status, a.error_type, a.error_message))
        if item_rows:
            self._conn.executemany(
                """INSERT INTO download_items
                   (lesson_id, item_type, name, status, error_type, error_message)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                item_rows,
            )

        self._conn.commit()

    def finish_session(self, session_id: int, status: str = "completed") -> None:
        self._conn.execute(
            "UPDATE download_sessions SET finished_at = ?, status = ? WHERE id = ?",
            (_now(), status, session_id),
        )
        self._conn.commit()

    def get_platforms(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT platform_name,
                      COUNT(*) as session_count,
                      MAX(started_at) as last_session
               FROM download_sessions
               GROUP BY platform_name
               ORDER BY last_session DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_sessions(self, platform_name: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE s.platform_name = ?" if platform_name else ""
        params = (platform_name,) if platform_name else ()
        rows = self._conn.execute(
            f"""SELECT s.*,
                       COUNT(l.id) as lesson_count,
                       SUM(CASE WHEN l.status = 'success' THEN 1 ELSE 0 END) as success_count,
                       SUM(CASE WHEN l.status = 'partial' THEN 1 ELSE 0 END) as partial_count,
                       SUM(CASE WHEN l.status = 'error' THEN 1 ELSE 0 END) as error_count,
                       SUM(CASE WHEN l.status = 'skipped' THEN 1 ELSE 0 END) as skipped_count
                FROM download_sessions s
                LEFT JOIN download_lessons l ON l.session_id = s.id
                {where}
                GROUP BY s.id
                ORDER BY s.started_at DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_courses_for_platform(self, platform_name: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT l.course_id,
                      l.course_name,
                      COUNT(*) as total_lessons,
                      SUM(CASE WHEN l.status = 'success' THEN 1 ELSE 0 END) as success_count,
                      SUM(CASE WHEN l.status = 'partial' THEN 1 ELSE 0 END) as partial_count,
                      SUM(CASE WHEN l.status = 'error' THEN 1 ELSE 0 END) as error_count,
                      SUM(CASE WHEN l.status = 'skipped' THEN 1 ELSE 0 END) as skipped_count,
                      MAX(l.completed_at) as last_download
               FROM download_lessons l
               JOIN download_sessions s ON s.id = l.session_id
               WHERE s.platform_name = ?
               GROUP BY l.course_id
               ORDER BY last_download DESC""",
            (platform_name,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_course_tree(self, platform_name: str, course_id: str) -> list[dict[str, Any]]:
        """Get all lessons for a course on a platform, ordered for tree display."""
        rows = self._conn.execute(
            """SELECT l.*
               FROM download_lessons l
               JOIN download_sessions s ON s.id = l.session_id
               WHERE s.platform_name = ? AND l.course_id = ?
               ORDER BY l.id""",
            (platform_name, course_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_lesson_by_id(self, lesson_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM download_lessons WHERE id = ?", (lesson_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_lesson_items(self, lesson_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM download_items WHERE lesson_id = ? ORDER BY id",
            (lesson_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_lessons(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM download_lessons WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as exc:
            logging.debug("Error closing history database: %s", exc)
