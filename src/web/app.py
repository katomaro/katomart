from __future__ import annotations

import argparse
import json
import logging
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, render_template, request, jsonify
from werkzeug.serving import make_server

from src.utils.history_manager import HistoryManager

_BRT = timezone(timedelta(hours=-3))


def _to_brt(iso_str: str | None) -> str:
    """Convert an ISO-8601 UTC timestamp to 'dd/mm/yyyy HH:MM:SS' in Brasilia time."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_BRT).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return iso_str


def _to_brt_date(iso_str: str | None) -> str:
    """Convert to 'dd/mm/yyyy' only."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_BRT).strftime("%d/%m/%Y")
    except Exception:
        return iso_str[:10] if iso_str else "N/A"


def create_app(db_path: Path, settings_path: Path | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
    app.config["DB_PATH"] = db_path
    app.config["SETTINGS_PATH"] = settings_path
    app.jinja_env.filters["brt"] = _to_brt
    app.jinja_env.filters["brt_date"] = _to_brt_date

    _transcription_status: dict[int, dict] = {}

    def _get_history() -> HistoryManager:
        return HistoryManager(app.config["DB_PATH"])

    def _load_settings() -> dict:
        sp = app.config.get("SETTINGS_PATH")
        if sp and Path(sp).exists():
            try:
                with open(sp, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @app.route("/")
    def index():
        hm = _get_history()
        platforms = hm.get_platforms()
        hm.close()
        return render_template("index.html", platforms=platforms, active_page="home")

    @app.route("/platform/<platform_name>")
    def platform_courses(platform_name: str):
        hm = _get_history()
        courses = hm.get_courses_for_platform(platform_name)
        hm.close()
        return render_template(
            "index.html",
            courses=courses,
            platform_name=platform_name,
            active_page="courses",
        )

    @app.route("/platform/<platform_name>/course/<path:course_id>")
    def course_detail(platform_name: str, course_id: str):
        hm = _get_history()
        lessons = hm.get_course_tree(platform_name, course_id)

        # Build tree: module -> lessons, and fetch items per lesson
        modules: dict[str, list[dict]] = {}
        for lesson in lessons:
            mod = lesson["module_name"]
            items = hm.get_lesson_items(lesson["id"])
            lesson["files"] = items
            modules.setdefault(mod, []).append(lesson)

        course_name = lessons[0]["course_name"] if lessons else course_id
        hm.close()
        return render_template(
            "index.html",
            modules=modules,
            course_name=course_name,
            platform_name=platform_name,
            active_page="course_detail",
        )

    @app.route("/api/sessions")
    def api_sessions():
        platform = request.args.get("platform")
        hm = _get_history()
        data = hm.get_sessions(platform_name=platform)
        hm.close()
        return jsonify(data)

    @app.route("/api/lessons/<int:session_id>")
    def api_lessons(session_id: int):
        hm = _get_history()
        data = hm.get_lessons(session_id)
        hm.close()
        return jsonify(data)

    @app.route("/api/courses")
    def api_courses():
        platform = request.args.get("platform")
        hm = _get_history()
        data = hm.get_courses_for_platform(platform) if platform else []
        hm.close()
        return jsonify(data)

    @app.route("/api/transcribe/<int:lesson_id>", methods=["POST"])
    def start_transcription(lesson_id: int):
        if lesson_id in _transcription_status and _transcription_status[lesson_id].get("status") == "running":
            return jsonify({"status": "running", "message": "Transcricao ja em andamento"})

        hm = _get_history()
        lesson = hm.get_lesson_by_id(lesson_id)
        hm.close()

        if not lesson:
            return jsonify({"status": "error", "message": "Aula nao encontrada"}), 404
        if not lesson.get("lesson_path"):
            return jsonify({"status": "error", "message": "Caminho da aula nao disponivel"}), 400

        settings = _load_settings()
        _transcription_status[lesson_id] = {"status": "running", "message": "Iniciando transcricao..."}

        def _run():
            try:
                from src.utils.transcription import transcribe_lesson
                results = transcribe_lesson(
                    lesson["lesson_path"],
                    ffmpeg_path=settings.get("ffmpeg_path"),
                    whisper_model=settings.get("whisper_model", "base"),
                    whisper_language=settings.get("whisper_language", "auto"),
                    output_format=settings.get("whisper_output_format", "srt"),
                )
                _transcription_status[lesson_id] = {
                    "status": "done",
                    "message": f"Arquivos gerados: {', '.join(results)}",
                }
            except Exception as exc:
                logging.error("Transcription failed for lesson %s: %s", lesson_id, exc)
                _transcription_status[lesson_id] = {
                    "status": "error",
                    "message": str(exc)[:300],
                }

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({"status": "started"})

    @app.route("/api/transcribe/<int:lesson_id>", methods=["GET"])
    def get_transcription_status(lesson_id: int):
        status = _transcription_status.get(lesson_id, {"status": "idle"})
        return jsonify(status)

    return app


class DashboardServer:
    """Non-blocking Flask server that runs in a daemon thread."""

    def __init__(self, db_path: Path, host: str = "127.0.0.1", port: int = 6102, settings_path: Path | None = None) -> None:
        self._host = host
        self._port = port
        self._app = create_app(db_path, settings_path)
        self._server = make_server(host, port, self._app, threaded=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        self._thread.start()
        logging.info("Dashboard server started at %s", self.url)

    def stop(self) -> None:
        self._server.shutdown()
        logging.info("Dashboard server stopped.")

    def open_browser(self) -> None:
        webbrowser.open(self.url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Katomart Download History Dashboard")
    parser.add_argument("--db", required=True, help="Path to katomart_history.db")
    parser.add_argument("--settings", default=None, help="Path to settings.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6102)
    args = parser.parse_args()

    settings_path = Path(args.settings) if args.settings else None
    server = DashboardServer(Path(args.db), args.host, args.port, settings_path=settings_path)
    webbrowser.open(server.url)
    server._server.serve_forever()
