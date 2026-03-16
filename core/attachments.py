from __future__ import annotations

from functools import lru_cache
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, List

from core.runtime import AGENT_DIR, WORKSPACE_DIR, safe_path
from llm_client.multimodal import create_attachment_ref


ATTACHMENT_TOKEN_RE = re.compile(r"(?<!\S)@(?:\"([^\"]+)\"|'([^']+)'|(\S+))")
ATTACHMENT_PARTIAL_RE = re.compile(r"(?<!\S)@(?:\"([^\"]*)\"|'([^']*)'|(\S*))$")
ATTACHMENT_START_RE = re.compile(r"(?<!\S)@")
ATTACHMENT_SUGGESTION_LIMIT = 8
ATTACHMENT_GLOBAL_SCAN_LIMIT = 5000
ATTACHMENT_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
ATTACHMENT_INCLUDE_AGENT_ROOT = (os.getenv("ZERO_CODE_ATTACHMENT_INCLUDE_AGENT_ROOT", "0").strip().lower() in {"1", "true", "yes", "on"})


def _iter_attachment_roots() -> list[Path]:
    roots: list[Path] = [WORKSPACE_DIR]
    if ATTACHMENT_INCLUDE_AGENT_ROOT and AGENT_DIR not in roots:
        roots.append(AGENT_DIR)
    return roots


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(WORKSPACE_DIR):
        return resolved.relative_to(WORKSPACE_DIR).as_posix()
    if resolved.is_relative_to(AGENT_DIR):
        return f"@agent/{resolved.relative_to(AGENT_DIR).as_posix()}"
    try:
        return resolved.as_posix()
    except Exception:
        return str(path)


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

    # Only fallback to global index when the scoped directory cannot be resolved.
    # This keeps bare '@' focused on the current directory instead of mixing nested files.
    allow_global_fallback = not explicit_directory_query and not resolved_scoped_directory
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

    # For bare '@' queries, prioritize files so they are not hidden behind many directories.
    if not prefix:
        candidates.sort(key=lambda item: (item[1]["kind"] == "dir", item[0][2].lower()))
    else:
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


def _resolve_attachment_file(raw_path: str) -> Path | None:
    try:
        resolved = safe_path(raw_path)
    except Exception:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _find_attachment_token_bounds(text: str, start_index: int) -> tuple[int, str] | None:
    cursor = start_index + 1
    if cursor >= len(text):
        return None

    quote = text[cursor]
    if quote in {'"', "'"}:
        closing_index = text.find(quote, cursor + 1)
        if closing_index < 0:
            return None
        return closing_index + 1, text[cursor + 1:closing_index]

    line_end = text.find("\n", cursor)
    if line_end < 0:
        line_end = len(text)
    segment = text[cursor:line_end]
    if not segment:
        return None

    end_positions = {len(segment)}
    for match in re.finditer(r"\s+", segment):
        end_positions.add(match.start())

    for end_pos in sorted((pos for pos in end_positions if pos > 0), reverse=True):
        candidate = segment[:end_pos].rstrip()
        if not candidate:
            continue
        if _resolve_attachment_file(candidate) is not None:
            return cursor + end_pos, candidate

    simple_match = re.match(r"\S+", segment)
    if simple_match:
        return cursor + simple_match.end(), simple_match.group(0)
    return None


def build_user_message(query: str) -> tuple[dict[str, Any], list[str]]:
    attachments: List[dict[str, Any]] = []
    warnings: List[str] = []

    cleaned_parts: List[str] = []
    cursor = 0
    for match in ATTACHMENT_START_RE.finditer(query):
        start = match.start()
        if start < cursor:
            continue

        token = _find_attachment_token_bounds(query, start)
        if token is None:
            continue

        end, raw_path = token
        cleaned_parts.append(query[cursor:start])

        resolved = _resolve_attachment_file(raw_path)
        if resolved is None:
            try:
                safe_path(raw_path)
                warnings.append(f"Skipping attachment `{raw_path}`: not a file")
            except Exception as exc:
                warnings.append(f"Skipping attachment `{raw_path}`: {exc}")
            cleaned_parts.append(query[start:end])
            cursor = end
            continue

        try:
            attachment = create_attachment_ref(resolved)
        except Exception as exc:
            warnings.append(f"Skipping attachment `{raw_path}`: {exc}")
            cleaned_parts.append(query[start:end])
            cursor = end
            continue

        if attachment.get("kind") not in {"image", "pdf"}:
            display = _display_path(Path(attachment["path"]))
            cleaned_parts.append(f"`{display}`")
            cursor = end
            continue

        attachments.append(attachment)
        cursor = end

    cleaned_parts.append(query[cursor:])
    cleaned_query = "".join(cleaned_parts)
    cleaned_query = re.sub(r"\s{2,}", " ", cleaned_query).strip()

    if not attachments:
        fallback_text = cleaned_query or query
        return {"role": "user", "content": fallback_text}, warnings

    attachment_paths = [
        _display_path(Path(str(attachment.get("path") or attachment.get("filename") or "attachment")))
        for attachment in attachments
    ]
    attachment_path_text = "\n".join(f"[Attached path: {path}]" for path in attachment_paths if path)
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