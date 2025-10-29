import requests
import json
import logging
import re
from typing import Any, Dict, List
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from src.platforms.base import BasePlatform
from src.config.settings_manager import SettingsManager
from src.downloaders.factory import DownloaderFactory
from src.downloaders.ytdlp_downloader import YtdlpDownloader

from src.utils.filesystem import sanitize_path_component
from src.utils.filesystem import truncate_component, truncate_filename_preserve_ext


class WorkerSignals(QObject):
    """
    Defines signals available from a running worker thread.
    Supported signals are:
    - finished: No data
    - error: tuple (exctype, value, traceback.format_exc())
    - result: object data returned from processing
    - progress: int indicating % progress
    """
    finished = Signal()
    error = Signal(tuple)
    result = Signal(str)
    progress = Signal(int)


class FetchCoursesWorker(QRunnable):
    """
    Worker to fetch, merge, and process the list of available courses.
    """

    def __init__(self, platform: BasePlatform, credentials: dict):
        super().__init__()
        self.platform = platform
        self.credentials = credentials
        self.signals = WorkerSignals()

    def run(self) -> None:
        """
        Authenticates and fetches courses using the provided platform.
        """
        try:
            logging.info("Worker: Autenticando e obtendo cursos...")
            self.platform.authenticate(self.credentials)
            courses = self.platform.fetch_courses()

            logging.info(f"Worker: Obtidos e processados {len(courses)} cursos.")
            courses_json = json.dumps(courses)
            self.signals.result.emit(courses_json)

        except requests.exceptions.RequestException as e:
            logging.error(f"Worker: Network Error - {e!r}")
            self.signals.error.emit((type(e), e, "A network error occurred."))
        except Exception as e:
            logging.error(f"Worker: An unexpected error occurred - {e!r}", exc_info=True)
            self.signals.error.emit((type(e), e, str(e)))
        finally:
            self.signals.finished.emit()


class FetchModulesWorker(QRunnable):
    """
    Worker to fetch module and lesson details for selected courses.
    """

    def __init__(self, platform: BasePlatform, courses: List[Dict[str, Any]]):
        super().__init__()
        self.platform = platform
        self.courses = courses
        self.signals = WorkerSignals()

    def run(self) -> None:
        """
        Fetches course content using the provided platform.
        """
        try:
            logging.info(f"Worker: Fetching content for {len(self.courses)} courses...")
            content = self.platform.fetch_course_content(self.courses)
            content_json = json.dumps(content)
            self.signals.result.emit(content_json)
        except Exception as e:
            logging.error(f"Worker: An unexpected error occurred - {e!r}", exc_info=True)
            self.signals.error.emit((type(e), e, str(e)))
        finally:
            self.signals.finished.emit()


class DownloadWorker(QRunnable):
    """
    Worker to download files for the selected modules and lessons.
    """

    def __init__(self, platform: BasePlatform, selection: Dict[str, Any], download_dir: str, settings_manager: SettingsManager):
        super().__init__()
        self.platform = platform
        self.selection = selection
        self.signals = WorkerSignals()
        self.download_dir = Path(download_dir)
        self.settings_manager = settings_manager
        self.settings = self.settings_manager.get_settings()

    def run(self) -> None:
        """
        Iterates through the selection, fetches lesson details, and prepares for download.
        """
        try:
            logging.info("Download iniciado.")
            session = self.platform.get_session()
            if not session:
                raise ConnectionError("Download worker requires an authenticated session.")

            total_lessons = sum(len(module.get("lessons", [])) for course in self.selection.values() for module in course.get("modules", []))
            lessons_processed = 0

            for course_id, course_data in self.selection.items():
                course_slug = course_data.get("slug")
                course_title = course_data.get("name", f"Curso-{course_id}")
                course_title = course_title.rsplit("] ", 1)[1]
                course_title = sanitize_path_component(course_title)
                course_title = truncate_component(course_title, getattr(self.settings, 'max_course_name_length', 40))
                course_path = self.download_dir / course_title
                course_path.mkdir(parents=True, exist_ok=True)
                self.signals.result.emit(f"Processando curso {course_title}")

                for module_index, module in enumerate(course_data.get("modules", []), start=1):
                    if module.get("download") is False:
                        self.signals.result.emit(f"  -> Pulando módulo não selecionado para download ou bloqueado: {module.get('title', 'Unknown Module')}")
                        continue
                    module_title = module.get("title", "Módulo sem titulo")
                    module_title = sanitize_path_component(module_title)
                    module_title = truncate_component(module_title, getattr(self.settings, 'max_module_name_length', 60))
                    module_order = module.get("order", module_index)
                    module_id = module.get("id")
                    module_title_full = f"{module_order}. {module_title}"
                    module_path = course_path / module_title_full
                    module_path.mkdir(parents=True, exist_ok=True)
                    self.signals.result.emit(f"  -> Modulo: {module_title}")

                    for lesson_index, lesson in enumerate(module.get("lessons", []), start=1):
                        if lesson.get("download") is False:
                            self.signals.result.emit(f"    - Pulando aula não selecionada para download ou bloqueada: {lesson.get('title', 'Unknown Lesson')}")
                            lessons_processed += 1
                            progress = int((lessons_processed / total_lessons) * 100) if total_lessons > 0 else 0
                            self.signals.progress.emit(progress)
                            continue
                        lesson_title = lesson.get("title", "Aula sem titulo")
                        lesson_title = sanitize_path_component(lesson_title)
                        lesson_title = truncate_component(lesson_title, getattr(self.settings, 'max_lesson_name_length', 60))
                        lesson_order = lesson.get("order", lesson_index)
                        lesson_title_full = f"{lesson_order}. {lesson_title}"
                        lesson_path = module_path / lesson_title_full
                        lesson_path.mkdir(parents=True, exist_ok=True)
                        try:
                            self.signals.result.emit(f"    - Obtendo detalhes para a aula: {lesson_title}")
                            lesson_details = self.platform.fetch_lesson_details(lesson, course_slug, course_id, module_id)

                            logging.info(f"Aula '{lesson_title}' conteúdo: "
                                            f"{len(lesson_details.videos)} vídeo(s), "
                                            f"{len(lesson_details.attachments)} anexo(s).")
                            
                            if lesson_details.description:
                                if lesson_details.description.description_type in ("text", "markdown"):
                                    description_path = lesson_path / "Descrição.txt"
                                else:
                                    description_path = lesson_path / "Descrição.html"
                                with open(description_path, 'w', encoding='utf-8') as desc_file:
                                    desc_file.write(lesson_details.description.text)
                                if self.settings.download_embedded_videos:
                                    html = lesson_details.description.text or ""
                                    found_urls = []
                                    found_urls.extend(re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, flags=re.I))
                                    found_urls.extend(re.findall(r'<video[^>]+src=["\']([^"\']+)["\']', html, flags=re.I))
                                    found_urls.extend(re.findall(r'<source[^>]+src=["\']([^"\']+)["\']', html, flags=re.I))
                                    found_urls.extend(re.findall(r'href=["\'](https?://[^"\']+)["\']', html, flags=re.I))
                                    found_urls.extend(re.findall(r'https?://[^\s"\'<>]+', html, flags=re.I))

                                    normalized = []
                                    for u in found_urls:
                                        if not u:
                                            continue
                                        if u.startswith('//'):
                                            u = 'https:' + u
                                        if u.startswith('javascript:') or u.startswith('mailto:') or u.startswith('#'):
                                            continue
                                        if u not in normalized:
                                            normalized.append(u)

                                    if normalized:
                                        for emb_idx, emb_url in enumerate(normalized, start=1):
                                            emb_name = f"{emb_idx}. Aula"
                                            emb_name = truncate_filename_preserve_ext(emb_name, getattr(self.settings, 'max_file_name_length', 30))
                                            emb_path = lesson_path / emb_name
                                            logging.info(f"Baixando vídeo linkado '{emb_url}' para '{emb_path}'")
                                            downloader = YtdlpDownloader(self.settings_manager)
                                            try:
                                                success = downloader.download_video(emb_url, self.platform.get_session(), emb_path)
                                                if success:
                                                    self.signals.result.emit(f"    - Vídeo linkado baixado: {emb_name}")
                                                else:
                                                    self.signals.result.emit(f"    - [ERROR] Falha ao baixar vídeo linkado: {emb_url}")
                                            except Exception as e:
                                                logging.error(f"Erro ao baixar vídeo linkado {emb_url}: {e}", exc_info=True)
                                                self.signals.result.emit(f"    - [ERROR] Falha ao baixar vídeo linkado: {emb_url}")
                                self.signals.result.emit(f"      - Descrição salva em {description_path}")

                            for video_index, video in enumerate(lesson_details.videos, start=1):
                                video_order = video.order or video_index
                                video_name = f"{video_order}. Aula"
                                video_name = truncate_filename_preserve_ext(video_name, getattr(self.settings, 'max_file_name_length', 30))
                                video_path = lesson_path / video_name
                                logging.info(f"Baixando Vídeo '{video_name}' para '{video_path}'")
                                downloader = DownloaderFactory.get_downloader(video.url, self.settings_manager)
                                downloader.download_video(video.url, self.platform.get_session(), video_path)
                                self.signals.result.emit(f"    - Vídeo baixado: {video_name}")

                            for attachment_index, attachment in enumerate(lesson_details.attachments, start=1):
                                attachment_order = attachment.order or attachment_index
                                full_attachment_name = sanitize_path_component(attachment.filename)
                                full_attachment_name = f"{attachment_order}. {full_attachment_name}"
                                full_attachment_name = truncate_filename_preserve_ext(full_attachment_name, getattr(self.settings, 'max_file_name_length', 30))
                                attachment_path = lesson_path / full_attachment_name
                                logging.info(f"Baixando Anexo '{attachment.filename}' para '{attachment_path}'")
                                self.platform.download_attachment(attachment, attachment_path, course_slug, course_id, module_id)
                                self.signals.result.emit(f"    - Anexo baixado: {attachment.filename}")
                            
                            if lesson_details.auxiliary_urls:
                                aux_path = lesson_path / f"Links Extras.txt"
                                with open(aux_path, 'w', encoding='utf-8') as aux_file:
                                    for aux_index, aux in enumerate(lesson_details.auxiliary_urls, start=1):
                                        aux_file.write(f"{aux_index}. {aux}\n")
                                self.signals.result.emit(f"      - URL auxiliar salva em {aux_path}")
                            
                        except Exception as e:
                            logging.error(f"Failed to fetch details for lesson '{lesson_title}': {e}")
                            self.signals.result.emit(f"    - [ERROR] Falha em obeter dados da aula: {lesson_title}")
                        
                        lessons_processed += 1
                        progress = int((lessons_processed / total_lessons) * 100) if total_lessons > 0 else 0
                        self.signals.progress.emit(progress)

            self.signals.result.emit("Processo de download concluído.")
            if total_lessons == 0:
                self.signals.progress.emit(100)

        except Exception as e:
            logging.error(f"An unexpected error occurred in DownloadWorker: {e}", exc_info=True)
            self.signals.error.emit((type(e), e, str(e)))
        finally:
            self.signals.finished.emit()
