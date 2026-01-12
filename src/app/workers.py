import json
import logging
import re
import subprocess
import time
import shutil
import html
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from PySide6.QtCore import QObject, QRunnable, Signal
from urllib.parse import urlparse

from src.platforms.base import BasePlatform
from src.config.settings_manager import SettingsManager
from src.downloaders.factory import DownloaderFactory
from src.utils.resume_manager import ResumeManager

from src.utils.filesystem import sanitize_path_component
from src.utils.filesystem import truncate_component, truncate_filename_preserve_ext, get_executable_path


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
    request_auth_confirmation = Signal(object)


class FetchCoursesWorker(QRunnable):
    """
    Worker to fetch, merge, and process the list of available courses.
    """

    def __init__(self, platform: BasePlatform, credentials: dict, query: str | None = None):
        super().__init__()
        self.platform = platform
        self.credentials = credentials
        self.query = query
        self.signals = WorkerSignals()

    def run(self) -> None:
        """
        Authenticates and fetches courses using the provided platform.
        """
        try:
            logging.info("Worker: Autenticando e obtendo cursos...")
            self.platform.authenticate(self.credentials)
            
            if self.query:
                logging.info(f"Worker: Pesquisando cursos com query: '{self.query}'")
                courses = self.platform.search_courses(self.query)
            else:
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

    def __init__(
        self,
        platform: BasePlatform,
        selection: Dict[str, Any],
        download_dir: str,
        settings_manager: SettingsManager,
        platform_name: str,
        selected_courses: list | None = None,
        resume_state: Dict[str, Any] | None = None,
    ):
        super().__init__()
        self.platform = platform
        self.selection = resume_state.get("selection", selection) if resume_state else selection
        self.signals = WorkerSignals()
        self.download_dir = Path(download_dir)
        self.settings_manager = settings_manager
        self.settings = self.settings_manager.get_settings()
        self._retry_attempts = max(0, getattr(self.settings, "download_retry_attempts", 0))
        self._retry_delay_seconds = max(0, getattr(self.settings, "download_retry_delay_seconds", 0))
        self._whisper_model = None
        self.platform_name = platform_name
        self.selected_courses = selected_courses or []
        self.resume_manager: ResumeManager | None = None
        self.resume_state: Dict[str, Any] | None = None

        if getattr(self.settings, "create_resume_summary", False):
            self.resume_manager = ResumeManager(self.download_dir)
            self.resume_state = resume_state
            if self.resume_manager and self.resume_state:
                self.resume_state.setdefault("selection", self.selection)
                self.resume_state.setdefault("selected_courses", self.selected_courses)
                self.resume_manager.save_state(self.platform_name, self.resume_state)

    def _run_with_retries(self, func, description: str, treat_false_as_failure: bool = True):
        """Execute ``func`` with the configured retry policy.

        Args:
            func: Callable to execute.
            description: Human friendly label for logging.
            treat_false_as_failure: When True, a ``False`` return value triggers
                a retry.

        Returns:
            The function result when successful.

        Raises:
            Exception: Propagates the last error after exhausting retries.
        """

        total_attempts = self._retry_attempts + 1
        for attempt in range(total_attempts):
            try:
                result = func()
                if treat_false_as_failure and result is False:
                    raise RuntimeError(f"{description} retornou status de falha.")
                return result
            except requests.exceptions.HTTPError as e:
                is_last_attempt = attempt >= self._retry_attempts

                if is_last_attempt:
                    if getattr(self.settings, "auto_reauth_on_error", False) and e.response.status_code in (400, 401):
                        creds = getattr(self.platform, 'credentials', {})
                        if creds and not creds.get("token"):
                            logging.warning(f"{description}: Erro {e.response.status_code} após esgotar tentativas. Tentando re-autenticação automática...")

                            confirmation_event = creds.get("manual_auth_confirmation")
                            if confirmation_event:
                                confirmation_event.clear()
                                self.signals.request_auth_confirmation.emit(confirmation_event)

                            try:
                                self.platform.refresh_auth()
                                logging.info("Re-autenticação bem sucedida. Tentando operação uma última vez...")
                                result = func()
                                if treat_false_as_failure and result is False:
                                    raise RuntimeError(f"{description} retornou status de falha após re-autenticação.")
                                logging.info(f"Operação '{description}' recuperada com sucesso após re-autenticação.")
                                return result
                            except Exception as auth_exc:
                                logging.error(f"Falha na re-autenticação ou na tentativa final: {auth_exc}")

                    logging.error(
                        f"{description} falhou após {self._retry_attempts} retentativas: {e}",
                        exc_info=True,
                    )
                    raise

                next_attempt = attempt + 2
                logging.warning(
                    f"{description} falhou (tentativa {next_attempt} de {total_attempts}). "
                    f"Nova tentativa em {self._retry_delay_seconds}s. Erro: {e}"
                )
                time.sleep(self._retry_delay_seconds)

            except Exception as exc:  # pragma: no cover - operational retry logic
                is_last_attempt = attempt >= self._retry_attempts
                if is_last_attempt:
                    logging.error(
                        f"{description} falhou após {self._retry_attempts} retentativas: {exc}",
                        exc_info=True,
                    )
                    raise

                next_attempt = attempt + 2
                logging.warning(
                    f"{description} falhou (tentativa {next_attempt} de {total_attempts}). "
                    f"Nova tentativa em {self._retry_delay_seconds}s. Erro: {exc}"
                )
                time.sleep(self._retry_delay_seconds)

    def _build_request_context(self, session: requests.Session) -> Dict[str, Any]:
        return {
            "headers": dict(session.headers),
            "cookies": session.cookies.get_dict(),
        }

    def _persist_resume_state(self) -> None:
        if self.resume_manager and self.resume_state:
            self.resume_manager.save_state(self.platform_name, self.resume_state)

    def _ensure_resume_state(self, session: requests.Session) -> None:
        if not self.resume_manager:
            return

        request_context = self._build_request_context(session)

        if not self.resume_state:
            self.resume_state = self.resume_manager.initialize_state(
                self.platform_name, self.selection, self.selected_courses, request_context
            )
        else:
            self.resume_state.setdefault("selection", self.selection)
            self.resume_state.setdefault("selected_courses", self.selected_courses)
            self.resume_state.setdefault("progress", {})
            self.resume_state.setdefault("completed", False)
            self.resume_state["request"] = request_context
            self._persist_resume_state()

    def _prepare_lesson_resume(
        self,
        course_id: str,
        module_key: str,
        lesson_key: str,
        lesson_details,
    ) -> Dict[str, Any] | None:
        if not self.resume_manager or not self.resume_state:
            return None

        return self.resume_manager.ensure_lesson_entry(
            self.resume_state,
            self.platform_name,
            course_id,
            module_key,
            lesson_key,
            lesson_details,
        )

    def _mark_resume_status(
        self,
        course_id: str,
        module_key: str,
        lesson_key: str,
        category: str,
        item_key: str | None,
        success: bool,
    ) -> None:
        if not self.resume_manager or not self.resume_state:
            return

        self.resume_manager.mark_status(
            self.resume_state,
            self.platform_name,
            course_id,
            module_key,
            lesson_key,
            category,
            item_key,
            success,
        )

    def _should_skip_download(
        self,
        lesson_entry: Dict[str, Any] | None,
        category: str,
        item_key: str | None = None,
    ) -> bool:
        if not lesson_entry:
            return False

        if category in {"description", "auxiliary_urls"}:
            return bool(lesson_entry.get(category))

        category_map = lesson_entry.get(category, {})
        if item_key is None:
            return False

        return bool(category_map.get(item_key))

    @staticmethod
    def _is_lesson_complete(lesson_entry: Dict[str, Any] | None) -> bool:
        if not lesson_entry:
            return False

        return all(
            [
                lesson_entry.get("description", True),
                lesson_entry.get("auxiliary_urls", True),
                all(lesson_entry.get("videos", {}).values()),
                all(lesson_entry.get("attachments", {}).values()),
            ]
        )

    def _find_downloaded_media(self, expected_path: Path) -> Optional[Path]:
        """Resolve the actual path of a downloaded media file.

        Some downloaders (like yt-dlp) may append extensions automatically. This
        helper first checks the exact expected path and then looks for files
        sharing the same stem within the same directory.
        """

        if expected_path.exists():
            return expected_path

        candidates = sorted(
            expected_path.parent.glob(expected_path.name + ".*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        return candidates[0] if candidates else None

    def _extract_audio_from_video(self, media_path: Path) -> Path:
        """Use ffmpeg to extract audio from a media file and return the audio path."""

        ffmpeg_exe = get_executable_path("ffmpeg", getattr(self.settings, "ffmpeg_path", None))
        if not ffmpeg_exe:
            raise FileNotFoundError("ffmpeg executable not found.")

        audio_path = media_path.with_suffix(".wav")
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            str(audio_path),
        ]

        logging.info(f"Extraindo áudio com ffmpeg: {' '.join(cmd)}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        return audio_path

    def _get_video_duration(self, video_path: Path) -> float:
        """Uses ffmpeg to get video duration in seconds."""
        ffmpeg_path = getattr(self.settings, "ffmpeg_path", None)
        ffmpeg_exe = get_executable_path("ffmpeg", ffmpeg_path)

        if not ffmpeg_exe:
            logging.warning("??? ffmpeg not found for duration check.")
            return 0.0
        cmd = [ffmpeg_exe, "-i", str(video_path)]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
            # Search for "Duration: 00:00:00.00"
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
            if match:
                hours, minutes, seconds = map(float, match.groups())
                return hours * 3600 + minutes * 60 + seconds
        except Exception as e:
            logging.error(f"Failed to get video duration: {e}")

        return 0.0

    def _load_whisper_model(self):
        if self._whisper_model is None:
            import whisper

            self._whisper_model = whisper.load_model(self.settings.whisper_model)
        return self._whisper_model

    def _transcribe_audio(self, audio_path: Path) -> Optional[Path]:
        """Generate a transcription for the provided audio file using Whisper."""

        from whisper.utils import get_writer

        model = self._load_whisper_model()
        language = None if self.settings.whisper_language == "auto" else self.settings.whisper_language

        if language and len(language) > 2 and "-" in language:
            language = language.split("-")[0].lower()

        result = model.transcribe(str(audio_path), language=language)

        output_format = self.settings.whisper_output_format or "srt"
        writer = get_writer(output_format, str(audio_path.parent))
        writer_opts = {"language": language} if language else {}
        writer(result, audio_path.stem, writer_opts)

        generated = audio_path.parent / f"{audio_path.stem}.{output_format}"
        return generated if generated.exists() else None

    def _maybe_transcribe_video(self, expected_media_path: Path) -> None:
        """Extract audio and transcribe a video when Whisper is enabled."""

        if not self.settings.use_whisper_transcription:
            return

        media_path = self._find_downloaded_media(expected_media_path)
        if not media_path:
            logging.warning(
                f"Não foi possível localizar o arquivo de mídia para transcrição: {expected_media_path}"
            )
            return

        try:
            audio_path = self._extract_audio_from_video(media_path)
            transcription_path = self._transcribe_audio(audio_path)

            if transcription_path:
                self.signals.result.emit(
                    f"      - Transcrição gerada com Whisper: {transcription_path.name}"
                )
            else:
                self.signals.result.emit(
                    "      - [WARNING] Whisper não gerou arquivo de transcrição."
                )
        except FileNotFoundError:
            logging.error("ffmpeg não encontrado. Certifique-se de que está instalado e no PATH.")
            self.signals.result.emit(
                "      - [ERROR] ffmpeg não encontrado para transcrição com Whisper."
            )
        except subprocess.CalledProcessError as exc:
            logging.error(f"Falha ao extrair áudio para Whisper: {exc}")
            self.signals.result.emit(
                "      - [ERROR] Falha ao extrair áudio para transcrição com Whisper."
            )
        except Exception as exc:
            logging.error(
                f"Erro inesperado durante transcrição com Whisper: {exc}",
                exc_info=True,
            )
            self.signals.result.emit(
                "      - [ERROR] Falha inesperada durante a transcrição com Whisper."
            )

    def run(self) -> None:
        """
        Iterates through the selection, fetches lesson details, and prepares for download.
        """
        try:
            logging.info("Download iniciado.")
            session = self.platform.get_session()
            if not session:
                raise ConnectionError("Download worker requires an authenticated session.")

            self._ensure_resume_state(session)

            total_lessons = sum(len(module.get("lessons", [])) for course in self.selection.values() for module in course.get("modules", []))
            lessons_processed = 0

            for course_id, course_data in self.selection.items():
                course_id_str = str(course_id)
                course_slug = course_data.get("slug")
                course_title = course_data.get("name", f"Curso-{course_id}")
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
                    module_key = (
                        ResumeManager._module_key(module, module_index)
                        if self.resume_manager
                        else str(module_id or module_order)
                    )
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

                        lesson_key = (
                            ResumeManager._lesson_key(lesson, lesson_index)
                            if self.resume_manager
                            else str(lesson.get("id") or lesson.get("order") or lesson_index)
                        )

                        if self.resume_state and self.resume_manager:
                            existing_entry = (
                                self.resume_state.get("progress", {})
                                .get(course_id_str, {})
                                .get("modules", {})
                                .get(module_key, {})
                                .get("lessons", {})
                                .get(lesson_key)
                            )
                            if self._is_lesson_complete(existing_entry):
                                self.signals.result.emit(
                                    f"    - Aula '{lesson.get('title', 'Unknown Lesson')}' já concluída anteriormente. Pulando downloads."
                                )
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
                        last_downloaded_video_path: Optional[Path] = None
                        try:
                            self.signals.result.emit(f"    - Obtendo detalhes para a aula: {lesson_title}")
                            lesson_details = self._run_with_retries(
                                lambda: self.platform.fetch_lesson_details(
                                    lesson, course_slug, course_id, module_id
                                ),
                                description=f"Obter dados da aula '{lesson_title}'",
                                treat_false_as_failure=False,
                            )

                            lesson_entry = self._prepare_lesson_resume(
                                course_id_str, module_key, lesson_key, lesson_details
                            )

                            logging.info(f"Aula '{lesson_title}' conteúdo: "
                                            f"{len(lesson_details.videos)} vídeo(s), "
                                            f"{len(lesson_details.attachments)} anexo(s).")

                            if lesson_details.description:
                                if self._should_skip_download(lesson_entry, "description"):
                                    self.signals.result.emit(
                                        "      - Descrição já registrada no resumo. Pulando download."
                                    )
                                else:
                                    description_path: Path
                                    if lesson_details.description.description_type in ("text", "markdown"):
                                        description_path = lesson_path / "Descrição.txt"
                                    else:
                                        description_path = lesson_path / "Descrição.html"
                                    try:
                                        with open(description_path, 'w', encoding='utf-8') as desc_file:
                                            desc_file.write(lesson_details.description.text)
                                        self._mark_resume_status(
                                            course_id_str, module_key, lesson_key, "description", None, True
                                        )
                                    except Exception as exc:
                                        logging.error("Falha ao salvar descrição: %s", exc, exc_info=True)
                                        self._mark_resume_status(
                                            course_id_str, module_key, lesson_key, "description", None, False
                                        )
                                        raise

                                    if self.settings.download_embedded_videos:
                                        desc_html = lesson_details.description.text or ""
                                        found_urls = []
                                        found_urls.extend(re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', desc_html, flags=re.I))
                                        found_urls.extend(re.findall(r'<video[^>]+src=["\']([^"\']+)["\']', desc_html, flags=re.I))
                                        found_urls.extend(re.findall(r'<source[^>]+src=["\']([^"\']+)["\']', desc_html, flags=re.I))
                                        found_urls.extend(re.findall(r'href=["\'](https?://[^"\']+)["\']', desc_html, flags=re.I))
                                        found_urls.extend(re.findall(r'https?://[^\s"\'<>]+', desc_html, flags=re.I))

                                        normalized = []
                                        for u in found_urls:
                                            if not u:
                                                continue
                                            u = html.unescape(u) 
                                            if u.startswith('//'):
                                                u = 'https:' + u
                                            if u.startswith('javascript:') or u.startswith('mailto:') or u.startswith('#'):
                                                continue
                                            if u not in normalized:
                                                normalized.append(u)

                                        if normalized:
                                            for emb_idx, emb_url in enumerate(normalized, start=1):
                                                emb_name = f"{emb_idx}. e_Aula"
                                                emb_name = truncate_filename_preserve_ext(emb_name, getattr(self.settings, 'max_file_name_length', 30))
                                                emb_path = lesson_path / emb_name
                                                logging.info(f"Baixando Conteudo linkado '{emb_url}' para '{emb_path}'")
                                                try:
                                                    parsed_emb = urlparse(emb_url)
                                                    emb_domain = (parsed_emb.netloc or "").lower()
                                                    if emb_domain.startswith("www."):
                                                        emb_domain = emb_domain[4:]
                                                except Exception:
                                                    emb_domain = ""

                                                blacklist: List[str] = getattr(self.settings, "embed_domain_blacklist", []) or []
                                                is_blacklisted = any(
                                                    emb_domain == b or emb_domain.endswith("." + b)
                                                    for b in blacklist
                                                )
                                                if is_blacklisted:
                                                    self.signals.result.emit(
                                                        f"    - [PULADO] URL embed blacklist: {emb_url}"
                                                    )
                                                    continue

                                                downloader = DownloaderFactory.get_downloader(emb_url, self.settings_manager)
                                                try:
                                                    extra_props = {}
                                                    if self.platform_name.lower() == "hotmart" and course_slug:
                                                        extra_props["referer"] = f"https://{course_slug}.club.hotmart.com/"

                                                    self._run_with_retries(
                                                        lambda: downloader.download_video(
                                                            emb_url, self.platform.get_session(), emb_path, extra_props=extra_props
                                                        ),
                                                        description=f"Download do Conteudo linkado '{emb_name}'",
                                                    )
                                                    self.signals.result.emit(f"    - Conteudo linkado baixado: {emb_name}")
                                                    self._maybe_transcribe_video(emb_path)
                                                except Exception as e:
                                                    logging.error(
                                                        f"Erro ao baixar Conteudo linkado {emb_url}: {e}",
                                                        exc_info=True,
                                                    )
                                                    self.signals.result.emit(
                                                        f"    - [ERROR] Falha ao baixar link encontrado (pode ser propaganda, etc): {emb_url}"
                                                    )
                                    self.signals.result.emit(f"      - Descrição salva em {description_path}")

                            if getattr(self.settings, "skip_video_download", False):
                                self.signals.result.emit("    - [CONFIG] Pulando download de vídeos principais (configuração ativa).")
                            else:
                                for video_index, video in enumerate(lesson_details.videos, start=1):
                                    video_order = video.order or video_index
                                    video_name = f"{video_order}. Aula"
                                    video_name = truncate_filename_preserve_ext(video_name, getattr(self.settings, 'max_file_name_length', 30))
                                    video_path = lesson_path / video_name
                                    logging.info(f"Baixando Vídeo '{video_name}' para '{video_path}'")
                                    downloader = DownloaderFactory.get_downloader(video.url, self.settings_manager)
                                    video_key = str(video.video_id or video_order)

                                    if self._should_skip_download(lesson_entry, "videos", video_key):
                                        self.signals.result.emit(
                                            f"    - Vídeo já baixado previamente pelo resumo: {video_name}"
                                        )
                                        continue

                                    try:
                                        extra_props = getattr(video, 'extra_props', {})

                                        self._run_with_retries(
                                            lambda: downloader.download_video(
                                                video.url, self.platform.get_session(), video_path, extra_props=extra_props
                                            ),
                                            description=f"Download do vídeo '{video_name}'",
                                        )
                                        last_downloaded_video_path = video_path
                                        self._mark_resume_status(
                                            course_id_str, module_key, lesson_key, "videos", video_key, True
                                        )
                                        self.signals.result.emit(f"    - Vídeo baixado: {video_name}")
                                        self._maybe_transcribe_video(video_path)
                                    except Exception:
                                        self._mark_resume_status(
                                            course_id_str, module_key, lesson_key, "videos", video_key, False
                                        )
                                        self.signals.result.emit(
                                            f"    - [ERROR] Falha ao baixar vídeo: {video_name}"
                                        )

                            for attachment_index, attachment in enumerate(lesson_details.attachments, start=1):
                                attachment_order = attachment.order or attachment_index
                                full_attachment_name = sanitize_path_component(attachment.filename)
                                full_attachment_name = f"{attachment_order}. {full_attachment_name}"
                                full_attachment_name = truncate_filename_preserve_ext(full_attachment_name, getattr(self.settings, 'max_file_name_length', 30))
                                attachment_path = lesson_path / full_attachment_name

                                allowed_exts = self.settings.allowed_attachment_extensions
                                if allowed_exts:
                                    normalized_exts = set()
                                    for ext in allowed_exts:
                                        ext = ext.strip().lower()
                                        if ext:
                                            if not ext.startswith("."):
                                                ext = "." + ext
                                            normalized_exts.add(ext)
                                    
                                    if normalized_exts:
                                        file_ext = (attachment.extension or "").strip().lower()
                                        if file_ext and not file_ext.startswith("."):
                                            file_ext = "." + file_ext

                                        if file_ext not in normalized_exts:
                                            self.signals.result.emit(
                                                f"    - [PULADO] Extensão não permitida: {attachment.filename}"
                                            )
                                            continue

                                logging.info(f"Baixando Anexo '{attachment.filename}' para '{attachment_path}'")
                                attachment_key = str(attachment.attachment_id or attachment_order)

                                if self._should_skip_download(lesson_entry, "attachments", attachment_key):
                                    self.signals.result.emit(
                                        f"    - Anexo já baixado previamente pelo resumo: {attachment.filename}"
                                    )
                                    continue

                                try:
                                    self._run_with_retries(
                                        lambda: self.platform.download_attachment(
                                            attachment, attachment_path, course_slug, course_id, module_id
                                        ),
                                        description=f"Download do anexo '{attachment.filename}'",
                                    )
                                    self._mark_resume_status(
                                        course_id_str, module_key, lesson_key, "attachments", attachment_key, True
                                    )
                                    self.signals.result.emit(f"    - Anexo baixado: {attachment.filename}")
                                except Exception:
                                    self._mark_resume_status(
                                        course_id_str, module_key, lesson_key, "attachments", attachment_key, False
                                    )
                                    self.signals.result.emit(
                                        f"    - [ERROR] Falha ao baixar anexo: {attachment.filename}"
                                    )

                            if lesson_details.auxiliary_urls:
                                aux_path = lesson_path / f"Links Extras.txt"
                                if self._should_skip_download(lesson_entry, "auxiliary_urls"):
                                    self.signals.result.emit(
                                        "      - Links extras já salvos anteriormente. Pulando geração do arquivo."
                                    )
                                else:
                                    try:
                                        with open(aux_path, 'w', encoding='utf-8') as aux_file:
                                            for aux_index, aux in enumerate(lesson_details.auxiliary_urls, start=1):
                                                if hasattr(aux, 'url'):
                                                    text = f"{aux.description or aux.title or 'Link'}: {aux.url}"
                                                else:
                                                    text = str(aux)
                                                aux_file.write(f"{aux_index}. {text}\n")
                                        self._mark_resume_status(
                                            course_id_str, module_key, lesson_key, "auxiliary_urls", None, True
                                        )
                                    except Exception as exc:
                                        logging.error("Falha ao salvar links extras: %s", exc, exc_info=True)
                                        self._mark_resume_status(
                                            course_id_str, module_key, lesson_key, "auxiliary_urls", None, False
                                        )
                                        raise
                                self.signals.result.emit(f"      - URL auxiliar salva em {aux_path}")
                            
                            watch_behavior = getattr(self.settings, "lesson_watch_status_behavior", "none")
                            mark_as_watched_bool = None
                            if watch_behavior == "watched":
                                mark_as_watched_bool = True
                            elif watch_behavior == "unwatched":
                                mark_as_watched_bool = False

                            if mark_as_watched_bool is not None:
                                self.signals.result.emit(f"      - Atualizando status para {'ASSISTIDO' if mark_as_watched_bool else 'NÃO ASSISTIDO'}...")
                                try:
                                    self._run_with_retries(
                                        lambda: self.platform.mark_lesson_watched(lesson, mark_as_watched_bool),
                                        description="Atualizar status da aula",
                                        treat_false_as_failure=False
                                    )
                                except Exception as status_exc:
                                     logging.error(f"Falha ao atualizar status da aula {lesson.get('title')}: {status_exc}")
                                     self.signals.result.emit(f"      - [AVISO] Falha ao atualizar status: {status_exc}")

                        except Exception as e:
                            logging.error(f"Failed to fetch details for lesson '{lesson_title}': {e}")
                            self._mark_resume_status(
                                course_id_str, module_key, lesson_key, "description", None, False
                            )
                            self.signals.result.emit(f"    - [ERROR] Falha em obter dados da aula: {lesson_title}")

                            if getattr(self.settings, "delete_folder_on_error", False):
                                try:
                                    if lesson_path.exists():
                                        shutil.rmtree(lesson_path)
                                        self.signals.result.emit(f"    - [INFO] Pasta da aula excluída devido ao erro: {lesson_path}")
                                except Exception as del_err:
                                    logging.error(f"Falha ao excluir pasta da aula {lesson_path}: {del_err}")
                                    self.signals.result.emit(f"    - [ERROR] Falha ao excluir pasta da aula: {del_err}")

                        if self.resume_manager and self.resume_state:
                            self.resume_state["completed"] = self.resume_manager.is_complete(
                                self.resume_state
                            )
                            self._persist_resume_state()

                        lessons_processed += 1
                        progress = int((lessons_processed / total_lessons) * 100) if total_lessons > 0 else 0
                        self.signals.progress.emit(progress)

                        delay_setting = getattr(self.settings, "lesson_access_delay", 0)
                        if delay_setting != 0:
                            wait_time = 0.0
                            if delay_setting > 0:
                                wait_time = float(delay_setting)
                            elif delay_setting == -1 and last_downloaded_video_path and last_downloaded_video_path.exists():
                                wait_time = self._get_video_duration(last_downloaded_video_path)
                            
                            if wait_time > 0:
                                self.signals.result.emit(f"    - Aguardando {wait_time:.1f}s antes da próxima aula...")
                                time.sleep(wait_time)

            self.signals.result.emit("Processo de download concluído.")
            if total_lessons == 0:
                self.signals.progress.emit(100)

        except Exception as e:
            logging.error(f"An unexpected error occurred in DownloadWorker: {e}", exc_info=True)
            self.signals.error.emit((type(e), e, str(e)))
        finally:
            self.signals.finished.emit()
