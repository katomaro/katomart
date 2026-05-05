from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from src.utils.filesystem import get_executable_path


def find_video_files(lesson_dir: Path) -> list[Path]:
    """Find video files in a lesson directory."""
    video_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts", ".m4v"}
    if not lesson_dir.exists():
        return []
    return sorted(
        (f for f in lesson_dir.iterdir() if f.is_file() and f.suffix.lower() in video_exts),
        key=lambda p: p.name,
    )


def extract_audio(media_path: Path, ffmpeg_path: str | None = None) -> Path:
    """Extract audio from a video file using ffmpeg. Returns path to WAV file."""
    ffmpeg_exe = get_executable_path("ffmpeg", ffmpeg_path)
    if not ffmpeg_exe:
        raise FileNotFoundError("ffmpeg executable not found.")

    audio_path = media_path.with_suffix(".wav")
    cmd = [
        ffmpeg_exe, "-y",
        "-i", str(media_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000",
        str(audio_path),
    ]
    logging.info("Extracting audio: %s", " ".join(cmd))
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    return audio_path


def transcribe_video(
    video_path: Path,
    ffmpeg_path: str | None = None,
    whisper_model: str = "base",
    whisper_language: str = "auto",
    output_format: str = "srt",
) -> Optional[Path]:
    """Extract audio and transcribe a single video file. Returns transcription path."""
    import whisper
    from whisper.utils import get_writer

    audio_path = extract_audio(video_path, ffmpeg_path)

    model = whisper.load_model(whisper_model)
    language = None if whisper_language == "auto" else whisper_language
    if language and len(language) > 2 and "-" in language:
        language = language.split("-")[0].lower()

    result = model.transcribe(str(audio_path), language=language)

    fmt = output_format or "srt"
    writer = get_writer(fmt, str(audio_path.parent))
    writer_opts = {"language": language} if language else {}
    writer(result, audio_path.stem, writer_opts)

    generated = audio_path.parent / f"{audio_path.stem}.{fmt}"
    return generated if generated.exists() else None


def transcribe_lesson(
    lesson_path: str,
    ffmpeg_path: str | None = None,
    whisper_model: str = "base",
    whisper_language: str = "auto",
    output_format: str = "srt",
) -> list[str]:
    """Transcribe all videos in a lesson directory. Returns list of generated file names."""
    lesson_dir = Path(lesson_path)
    videos = find_video_files(lesson_dir)
    if not videos:
        raise FileNotFoundError(f"No video files found in {lesson_dir}")

    results = []
    for video in videos:
        out = transcribe_video(video, ffmpeg_path, whisper_model, whisper_language, output_format)
        if out:
            results.append(out.name)
    return results
