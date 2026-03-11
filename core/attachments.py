from __future__ import annotations

from functools import lru_cache
import mimetypes
import re
from pathlib import Path
from typing import Any, List

from core.runtime import AGENT_DIR, WORKDIR, safe_path
from llm_client.multimodal import create_attachment_ref


ATTACHMENT_TOKEN_RE = re.compile(r"(?<!\S)@(?:\"([^\"]+)\"|'([^']+)'|(\S+))")
ATTACHMENT_PARTIAL_RE = re.compile(r"(?<!\S)@(?:\"([^\"]*)\"|'([^']*)'|(\S*))$")
ATTACHMENT_SUGGESTION_LIMIT = 8
ATTACHMENT_GLOBAL_SCAN_LIMIT = 5000
ATTACHMENT_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}


def _iter_attachment_roots() -> list[Path]:
    roots: list[Path] = []
    for root in (WORKDIR, AGENT_DIR):
        if root not in roots:
            roots.append(root)
    return roots


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(WORKDIR).as_posix()
    except Exception:
        try:
            return path.resolve().relative_to(AGENT_DIR).as_posix()
        except Exception:
            return path.resolve().as_posix()


def _build_suggestion_label(path: Path, value: str, kind: str) -> str:
    if kind == "dir":
        return f"DIR  {value}"

    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type == "application/pdf":
        return f"PDF  {value}"
    if mime_type and mime_type.startswith("image/"):
        return f"IMG  {value}"
    return f"FILE {value}"


def _is_top_level_directory_candidate(value: str, kind: str) -> bool:
    if kind != "dir":
        return False
    return "/" not in value[:-1]


@lru_cache(maxsize=1)
def _global_attachment_index() -> tuple[tuple[str, str], ...]:
    indexed: list[tuple[str, str]] = []
    seen: set[str] = set()

    for root in _iter_attachment_roots():
        count = 0
        for path in root.rglob("*"):
            if count >= ATTACHMENT_GLOBAL_SCAN_LIMIT:
                break
            if any(part in ATTACHMENT_SKIP_DIRS for part in path.parts):
                continue

            display = _display_path(path)
            value = display + "/" if path.is_dir() else display
            if value in seen:
                continue
            seen.add(value)
            indexed.append((value, "dir" if path.is_dir() else "file"))
            count += 1

    return tuple(indexed)


def _normalize_partial_path(raw_path: str) -> tuple[Path, str]:
    candidate = Path(raw_path).expanduser()
    if candidate.is_absolute():
        anchor = candidate.parent if raw_path and not raw_path.endswith(("/", "\\")) else candidate
        prefix = "" if raw_path.endswith(("/", "\\")) else candidate.name
        return anchor, prefix

    base = Path(raw_path)
    anchor = base.parent if raw_path and not raw_path.endswith("/") else base
    prefix = "" if raw_path.endswith("/") else base.name
    return anchor, prefix


def get_attachment_query_at_cursor(text: str, cursor_index: int | None = None) -> str | None:
    if cursor_index is None:
        cursor_index = len(text)
    text_before_cursor = text[:cursor_index]
    match = ATTACHMENT_PARTIAL_RE.search(text_before_cursor)
    if not match:
        return None
    return match.group(1) or match.group(2) or match.group(3) or ""


def _subsequence_score(query: str, candidate: str) -> int | None:
    if not query:
        return 0

    query = query.lower()
    candidate = candidate.lower()
    pos = -1
    gap_penalty = 0
    for char in query:
        next_pos = candidate.find(char, pos + 1)
        if next_pos < 0:
            return None
        if pos >= 0:
            gap_penalty += next_pos - pos - 1
        pos = next_pos
    return gap_penalty


def _match_attachment_candidate(query: str, entry_name: str, display_path: str) -> tuple[int, int, str] | None:
    if not query:
        return (0, 0, display_path)

    lowered_query = query.lower()
    lowered_name = entry_name.lower()
    lowered_display = display_path.lower()

    if lowered_name.startswith(lowered_query):
        return (0, len(entry_name), display_path)
    if lowered_display.startswith(lowered_query):
        return (1, len(display_path), display_path)
    if lowered_query in lowered_name:
        return (2, lowered_name.index(lowered_query), display_path)
    if lowered_query in lowered_display:
        return (3, lowered_display.index(lowered_query), display_path)

    subsequence_name = _subsequence_score(lowered_query, lowered_name)
    if subsequence_name is not None:
        return (4, subsequence_name, display_path)

    subsequence_display = _subsequence_score(lowered_query, lowered_display)
    if subsequence_display is not None:
        return (5, subsequence_display, display_path)

    return None


def get_attachment_suggestions(raw_path: str, limit: int = ATTACHMENT_SUGGESTION_LIMIT) -> list[dict[str, str]]:
    candidates: list[tuple[tuple[int, int, str], dict[str, str]]] = []
    seen: set[str] = set()
    anchor, prefix = _normalize_partial_path(raw_path)
    explicit_directory_query = raw_path.endswith(("/", "\\"))
    has_scoped_directory_query = anchor not in (Path(""), Path(".")) or anchor.is_absolute()
    resolved_scoped_directory = False

    for root in _iter_attachment_roots():
        base_dir = (root / anchor).resolve() if not anchor.is_absolute() else anchor.resolve()
        try:
            safe_dir = safe_path(str(base_dir))
        except Exception:
            continue
        if not safe_dir.exists() or not safe_dir.is_dir():
            continue
        resolved_scoped_directory = True

        try:
            entries = sorted(
                safe_dir.iterdir(),
                key=lambda path: (not path.is_dir(), path.name.lower()),
            )
        except Exception:
            continue

        for entry in entries:
            display = _display_path(entry)

            value = display + "/" if entry.is_dir() else display
            if value in seen:
                continue

            kind = "dir" if entry.is_dir() else "file"
            rank = _match_attachment_candidate(prefix, entry.name, value)
            if rank is None:
                continue

            seen.add(value)
            candidates.append((rank, {"value": value, "kind": kind, "label": _build_suggestion_label(entry, value, kind)}))

    allow_global_fallback = not explicit_directory_query and not (has_scoped_directory_query and resolved_scoped_directory)
    if len(candidates) < limit and allow_global_fallback:
        for value, kind in _global_attachment_index():
            if value in seen:
                continue
            if kind == "dir" and not _is_top_level_directory_candidate(value, kind):
                continue
            entry_name = value[:-1].split("/")[-1] if kind == "dir" else value.split("/")[-1]
            rank = _match_attachment_candidate(prefix, entry_name, value)
            if rank is None:
                continue
            seen.add(value)
            entry_path = Path(value[:-1] if kind == "dir" else value)
            candidates.append((rank, {"value": value, "kind": kind, "label": _build_suggestion_label(entry_path, value, kind)}))

    candidates.sort(key=lambda item: (item[0][0], item[0][1], item[1]["kind"] != "dir", item[0][2]))
    return [item[1] for item in candidates[:limit]]


def apply_attachment_suggestion(text: str, suggestion: str, cursor_index: int | None = None) -> str:
    if cursor_index is None:
        cursor_index = len(text)
    text_before_cursor = text[:cursor_index]
    text_after_cursor = text[cursor_index:]
    match = ATTACHMENT_PARTIAL_RE.search(text_before_cursor)
    if not match:
        return text

    quote = '"' if match.group(1) is not None else ("'" if match.group(2) is not None else "")
    replacement = f"@{quote}{suggestion}{quote}"
    if not suggestion.endswith("/"):
        replacement += " "

    return text_before_cursor[:match.start()] + replacement + text_after_cursor


def apply_attachment_parent_navigation(text: str, cursor_index: int | None = None) -> str:
    if cursor_index is None:
        cursor_index = len(text)
    text_before_cursor = text[:cursor_index]
    text_after_cursor = text[cursor_index:]
    match = ATTACHMENT_PARTIAL_RE.search(text_before_cursor)
    if not match:
        return text

    raw_path = match.group(1) or match.group(2) or match.group(3) or ""
    if not raw_path.endswith(("/", "\\")):
        return text

    quote = '"' if match.group(1) is not None else ("'" if match.group(2) is not None else "")
    normalized = raw_path.rstrip("/\\")
    if not normalized:
        replacement = f"@{quote}"
    else:
        parent = Path(normalized).parent.as_posix()
        replacement = f"@{quote}"
        if parent and parent != ".":
            replacement += parent + "/"

    return text_before_cursor[:match.start()] + replacement + text_after_cursor


def build_user_message(query: str) -> tuple[dict[str, Any], list[str]]:
    attachments: List[dict[str, Any]] = []
    warnings: List[str] = []

    def _replace(match: re.Match[str]) -> str:
        raw_path = match.group(1) or match.group(2) or match.group(3) or ""
        try:
            resolved = safe_path(raw_path)
        except Exception as exc:
            warnings.append(f"Skipping attachment `{raw_path}`: {exc}")
            return match.group(0)

        if not resolved.is_file():
            warnings.append(f"Skipping attachment `{raw_path}`: not a file")
            return match.group(0)

        try:
            attachment = create_attachment_ref(resolved)
        except Exception as exc:
            warnings.append(f"Skipping attachment `{raw_path}`: {exc}")
            return match.group(0)

        if attachment.get("kind") not in {"image", "pdf"}:
            return raw_path

        attachments.append(attachment)
        return ""

    cleaned_query = ATTACHMENT_TOKEN_RE.sub(_replace, query)
    cleaned_query = re.sub(r"\s{2,}", " ", cleaned_query).strip()

    if not attachments:
        fallback_text = cleaned_query or query
        return {"role": "user", "content": fallback_text}, warnings

    attachment_paths = [
        _display_path(Path(str(attachment.get("path") or attachment.get("filename") or "attachment")))
        for attachment in attachments
    ]
    # attachment_path_text = "\n".join(f"[Attached path: {path}]" for path in attachment_paths if path)
    attachment_path_text = None
    content: List[dict[str, Any]] = []
    if cleaned_query and attachment_path_text:
        content.append({"type": "text", "text": f"{cleaned_query}\n\n{attachment_path_text}"})
    elif cleaned_query:
        content.append({"type": "text", "text": cleaned_query})
    elif attachment_path_text:
        content.append({"type": "text", "text": attachment_path_text})
    else:
        content.append({"type": "text", "text": "Please analyze the attached file(s)."})

    for attachment in attachments:
        content.append({"type": "attachment", "attachment": attachment})

    return {"role": "user", "content": content}, warnings


def message_preview_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")

    text_parts: List[str] = []
    attached: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue
        if block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                text_parts.append(text)
        elif block.get("type") == "attachment":
            attachment = block.get("attachment") or {}
            name = attachment.get("filename") or "attachment"
            attached.append(str(name))

    preview = "\n\n".join(text_parts).strip()
    if attached:
        suffix = "\n\nAttachments: " + ", ".join(attached)
        preview = (preview + suffix).strip()
    return preview