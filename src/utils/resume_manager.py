from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from src.utils.filesystem import sanitize_path_component


class ResumeManager:
    """Persists and restores download progress for resume support."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    @staticmethod
    def _module_key(module_data: Dict[str, Any], default_index: int | None) -> str:
        return str(
            module_data.get("id")
            or module_data.get("order")
            or default_index
            or module_data.get("title")
            or "unknown-module"
        )

    @staticmethod
    def _lesson_key(lesson_data: Dict[str, Any], default_index: int | None) -> str:
        return str(
            lesson_data.get("id")
            or lesson_data.get("order")
            or default_index
            or lesson_data.get("title")
            or "unknown-lesson"
        )

    def _resume_path(self, platform_name: str) -> Path:
        safe_name = sanitize_path_component(platform_name) or "plataforma"
        return self._base_dir / f"{safe_name}.json"

    def load_state(self, platform_name: str) -> Dict[str, Any] | None:
        path = self._resume_path(platform_name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - operational log
            logging.warning("Falha ao carregar JSON de resumo em %s: %s", path, exc)
            return None

    def save_state(self, platform_name: str, state: Dict[str, Any]) -> None:
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            with open(self._resume_path(platform_name), "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)
        except OSError as exc:  # pragma: no cover - filesystem failure
            logging.error("Falha ao salvar JSON de resumo: %s", exc)

    def initialize_state(
        self,
        platform_name: str,
        selection: Dict[str, Any],
        selected_courses: list | None,
        request_context: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        state = {
            "platform": platform_name,
            "selection": selection,
            "selected_courses": selected_courses or [],
            "progress": {},
            "completed": False,
            "request": request_context or {},
        }
        self.save_state(platform_name, state)
        return state

    def update_request_context(
        self, state: Dict[str, Any], platform_name: str, request_context: Dict[str, Any]
    ) -> None:
        state["request"] = request_context
        self.save_state(platform_name, state)

    def ensure_lesson_entry(
        self,
        state: Dict[str, Any],
        platform_name: str,
        course_id: str,
        module_key: str,
        lesson_key: str,
        lesson_details: Any,
    ) -> Dict[str, Any]:
        progress = state.setdefault("progress", {})
        course_progress = progress.setdefault(str(course_id), {})
        modules = course_progress.setdefault("modules", {})
        module_progress = modules.setdefault(module_key, {})
        lessons = module_progress.setdefault("lessons", {})

        has_description = bool(lesson_details.description)
        has_auxiliary = bool(lesson_details.auxiliary_urls)

        lesson_entry = lessons.setdefault(
            lesson_key,
            {
                "description": not has_description,
                "auxiliary_urls": not has_auxiliary,
                "videos": {},
                "attachments": {},
            },
        )

        if "description" not in lesson_entry:
            lesson_entry["description"] = not has_description
        if "auxiliary_urls" not in lesson_entry:
            lesson_entry["auxiliary_urls"] = not has_auxiliary

        for video in lesson_details.videos:
            video_key = str(video.video_id or video.order)
            lesson_entry.setdefault("videos", {}).setdefault(video_key, False)

        for attachment in lesson_details.attachments:
            attachment_key = str(attachment.attachment_id or attachment.order)
            lesson_entry.setdefault("attachments", {}).setdefault(attachment_key, False)

        self.save_state(platform_name, state)
        return lesson_entry

    def mark_status(
        self,
        state: Dict[str, Any],
        platform_name: str,
        course_id: str,
        module_key: str,
        lesson_key: str,
        category: str,
        item_key: str | None,
        success: bool,
    ) -> None:
        progress = state.setdefault("progress", {})
        course_progress = progress.setdefault(str(course_id), {}).setdefault("modules", {})
        module_progress = course_progress.setdefault(module_key, {}).setdefault("lessons", {})
        lesson_entry = module_progress.setdefault(
            lesson_key,
            {"description": False, "auxiliary_urls": False, "videos": {}, "attachments": {}},
        )

        if category in {"videos", "attachments"} and item_key is not None:
            lesson_entry.setdefault(category, {})[item_key] = success
        elif category in {"description", "auxiliary_urls"}:
            lesson_entry[category] = success

        state["completed"] = self.is_complete(state)
        self.save_state(platform_name, state)

    def is_complete(self, state: Dict[str, Any]) -> bool:
        selection = state.get("selection", {})
        progress = state.get("progress", {})

        for course_id, course_data in selection.items():
            for module_index, module in enumerate(course_data.get("modules", []), start=1):
                if module.get("download") is False:
                    continue

                module_key = self._module_key(module, module_index)
                module_progress = progress.get(str(course_id), {}).get("modules", {}).get(module_key, {})
                lessons_progress = module_progress.get("lessons", {})

                for lesson_index, lesson in enumerate(module.get("lessons", []), start=1):
                    if lesson.get("download") is False:
                        continue

                    lesson_key = self._lesson_key(lesson, lesson_index)
                    lesson_entry = lessons_progress.get(lesson_key)
                    if not lesson_entry:
                        return False

                    if not all(
                        [
                            lesson_entry.get("description", True),
                            lesson_entry.get("auxiliary_urls", True),
                            all(lesson_entry.get("videos", {}).values()),
                            all(lesson_entry.get("attachments", {}).values()),
                        ]
                    ):
                        return False

        return True

