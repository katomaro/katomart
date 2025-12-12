import re
from typing import Set
from pathlib import Path

_INVALID_WIN_CHARS_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')

_RESERVED_WIN_NAMES: Set[str] = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def sanitize_path_component(name: str, replacement: str = "_") -> str:
    """
    Sanitizes a string to be a safe component in a Windows file path.

    This function performs the following actions:
    1. Replaces invalid Windows path characters with a specified replacement string.
    2. Removes any trailing periods or whitespace, which are not allowed by Windows.
    3. Checks if the resulting name is a reserved Windows filename (e.g., "CON").
       If it is, it prepends an underscore to the name.
    4. Ensures the resulting name is not empty, returning a replacement if it is.

    Args:
        name: The input string to sanitize.
        replacement: The string to use for replacing invalid characters. Defaults to "_".

    Returns:
        A sanitized string that is safe to use as a file or directory name on Windows.
    """
    sanitized_name = _INVALID_WIN_CHARS_RE.sub(replacement, name)

    sanitized_name = sanitized_name.rstrip(" .")

    if sanitized_name.upper() in _RESERVED_WIN_NAMES:
        sanitized_name = replacement + sanitized_name

    if not sanitized_name:
        return replacement

    return sanitized_name


def truncate_component(name: str, max_len: int) -> str:
    """Truncates a path component to at most max_len characters."""
    if max_len is None or max_len <= 0:
        return name
    if len(name) <= max_len:
        return name
    return name[:max_len].rstrip()


def truncate_filename_preserve_ext(filename: str, max_len: int, replacement: str = "_") -> str:
    """Truncates a filename preserving its extension(s) up to max_len characters.

    The function sanitizes the name first, then ensures the total length (including
    extension) does not exceed max_len. If the extension itself would exceed the
    max length, it is preserved and the stem is reduced to fit at least one
    character.
    """

    if max_len is None or max_len <= 0:
        return filename

    p = Path(filename)
    stem = sanitize_path_component(p.stem, replacement)
    suffix = "".join(p.suffixes)

    if not suffix:
        return truncate_component(stem, max_len)

    allowed_stem = max_len - len(suffix)
    if allowed_stem <= 0:
        truncated = (stem + suffix)[-max_len:]
        return sanitize_path_component(truncated, replacement)

    if len(stem) > allowed_stem:
        stem = stem[:allowed_stem]

    result = stem + suffix
    return sanitize_path_component(result, replacement)
