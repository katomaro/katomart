import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from src.utils.filesystem import get_executable_path

_RESERVED_FLAGS = {"-i", "-y", "-n"}


def _parse_ffmpeg_args(args_str: str) -> list[str]:
    """Parse a user-provided ffmpeg args string, rejecting reserved flags."""
    tokens = shlex.split(args_str or "", posix=(os.name != "nt"))
    for token in tokens:
        if token in _RESERVED_FLAGS:
            raise ValueError(
                f"O argumento '{token}' é gerenciado automaticamente e não pode ser informado."
            )
    return tokens


def run_ffmpeg_post_process(
    media_path: Path,
    ffmpeg_path_setting: Optional[str],
    args_str: str,
) -> Path:
    """Run ffmpeg on media_path using user-supplied args and replace the file.

    Produces a sibling file with suffix ``.ffmpeg.<ext>``, runs ffmpeg to
    transform the input into that temp file, then atomically moves it back
    over the original. The output keeps the same extension as the input.

    Raises FileNotFoundError if ffmpeg is unavailable, ValueError for invalid
    args, and subprocess.CalledProcessError if ffmpeg exits non-zero.
    """
    if not media_path.exists() or not media_path.is_file():
        raise FileNotFoundError(f"Arquivo de mídia não encontrado: {media_path}")

    ffmpeg_exe = get_executable_path("ffmpeg", ffmpeg_path_setting)
    if not ffmpeg_exe:
        raise FileNotFoundError("ffmpeg executable not found.")

    user_args = _parse_ffmpeg_args(args_str)
    if not user_args:
        raise ValueError("Nenhum argumento de ffmpeg foi informado.")

    suffix = media_path.suffix or ".mp4"
    temp_output = media_path.with_name(f"{media_path.stem}.ffmpeg{suffix}")

    if temp_output.exists():
        try:
            temp_output.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Não foi possível remover arquivo temporário pré-existente: {temp_output}"
            ) from exc

    cmd = [ffmpeg_exe, "-y", "-i", str(media_path), *user_args, str(temp_output)]

    logging.info("Pós-processamento ffmpeg: %s", " ".join(shlex.quote(c) for c in cmd))

    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or b"").decode("utf-8", errors="replace")[-600:]
        logging.error("ffmpeg pós-processamento falhou: %s", stderr_tail.strip())
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError:
                pass
        raise

    if not temp_output.exists() or temp_output.stat().st_size == 0:
        raise RuntimeError("ffmpeg terminou sem erro porém não produziu saída válida.")

    os.replace(temp_output, media_path)
    return media_path
