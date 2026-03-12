from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from core.attachments import (
    apply_attachment_parent_navigation,
    apply_attachment_suggestion,
    get_attachment_query_at_cursor,
    get_attachment_suggestions,
)
from core.commands import rewrite_attach_command
from core.tui import _is_browser_openable_path
from llm_client.capabilities import capability_overrides_from_env, resolve_model_capabilities
from llm_client.llm_factory import _format_messages_for_debug
from llm_client.multimodal import create_attachment_ref, render_message_content


def test_resolve_model_capabilities_for_known_vision_model():
    caps = resolve_model_capabilities("gpt-4o", "https://api.openai.com/v1")

    assert caps.provider == "openai"
    assert caps.supports_image_input is True
    assert caps.supports_pdf_input_chat is False
    assert caps.supports_pdf_input_responses is True


def test_resolve_model_capabilities_does_not_infer_provider_from_base_url_for_unknown_model():
    caps = resolve_model_capabilities("some-unknown-model", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    assert caps.provider == "openai-compatible"
    assert caps.supports_image_input is False


def test_capability_overrides_from_env_parses_boolean_flags():
    overrides = capability_overrides_from_env(
        {
            "OPENAI_COMPAT_SUPPORTS_IMAGE_INPUT": "true",
            "OPENAI_COMPAT_SUPPORTS_PDF_INPUT_CHAT": "1",
            "OPENAI_COMPAT_SUPPORTS_PDF_INPUT_RESPONSES": "false",
            "OPENAI_COMPAT_SUPPORTS_DATA_URL": "0",
        }
    )

    assert overrides == {
        "supports_image_input": True,
        "supports_pdf_input_chat": True,
        "supports_pdf_input_responses": False,
        "supports_data_url": False,
    }


def test_resolve_model_capabilities_applies_force_overrides_for_unknown_model():
    caps = resolve_model_capabilities(
        "some-unknown-model",
        "https://example.com/v1",
        {"supports_image_input": True, "supports_data_url": True},
    )

    assert caps.provider == "openai-compatible"
    assert caps.supports_image_input is True
    assert caps.supports_data_url is True


def test_rewrite_attach_command_maps_to_attachment_syntax():
    assert rewrite_attach_command("/attach docs/report.pdf summarize") == "@docs/report.pdf summarize"
    assert rewrite_attach_command('/attach "docs/My File.pdf" summarize') == '@"docs/My File.pdf" summarize'
    assert rewrite_attach_command("/attach") is None


@pytest.mark.parametrize(
    ("model", "base_url", "provider", "supports_image_input"),
    [
        ("deepseek-vl2", "https://api.deepseek.com/v1", "deepseek", True),
        ("qwen3-vl-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1", "dashscope", True),
        ("glm-4.1v-thinking", "https://open.bigmodel.cn/api/paas/v4/", "zhipu", True),
        ("kimi-vl-a3b-thinking", "https://api.moonshot.cn/v1", "moonshot", True),
        ("minimax-vl-01", "https://api.minimax.chat/v1", "minimax", True),
        ("deepseek-chat", "https://api.deepseek.com/v1", "deepseek", False),
        ("moonshot-v1-32k", "https://api.moonshot.cn/v1", "moonshot", False),
        ("abab6.5-chat", "https://api.minimax.chat/v1", "minimax", False),
        ("doubao-vision-pro", "https://ark.cn-beijing.volces.com/api/v3", "volcengine", True),
        ("glm-4-plus", "https://open.bigmodel.cn/api/paas/v4/", "zhipu", False),
    ],
)
def test_resolve_model_capabilities_for_additional_model_families(
    model,
    base_url,
    provider,
    supports_image_input,
):
    caps = resolve_model_capabilities(model, base_url)

    assert caps.provider == provider
    assert caps.supports_image_input is supports_image_input


def test_render_message_content_renders_native_image(tmp_path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"fakepngdata")
    attachment = create_attachment_ref(image_path)
    caps = resolve_model_capabilities("gpt-4o", "https://api.openai.com/v1")

    rendered = render_message_content(
        [
            {"type": "text", "text": "Describe this image."},
            {"type": "attachment", "attachment": attachment},
        ],
        role="user",
        capabilities=caps,
    )

    assert isinstance(rendered, list)
    assert rendered[0]["type"] == "text"
    assert rendered[1]["type"] == "image_url"
    assert rendered[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_format_messages_for_debug_keeps_image_shape_and_truncates_data_url():
    formatted = _format_messages_for_debug(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abcdefg"}},
                ],
            }
        ]
    )

    assert '"type": "image_url"' in formatted
    assert 'data:image/png;base64,<base64:7 chars>' in formatted
    assert 'data:image/png;base64,abcdefg' not in formatted


def test_render_message_content_falls_back_for_pdf(monkeypatch, tmp_path):
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    attachment = create_attachment_ref(pdf_path)

    multimodal = importlib.import_module("llm_client.multimodal")
    monkeypatch.setattr(multimodal, "_extract_pdf_text", lambda path: "[Page 1]\nImportant content")

    caps = resolve_model_capabilities("gpt-4o", "https://api.openai.com/v1")
    rendered = render_message_content(
        [
            {"type": "text", "text": "Summarize the PDF."},
            {"type": "attachment", "attachment": attachment},
        ],
        role="user",
        capabilities=caps,
    )

    assert isinstance(rendered, str)
    assert "Summarize the PDF." in rendered
    assert "[Attached PDF: report.pdf]" in rendered
    assert "Important content" in rendered


def test_build_user_message_parses_attachment_tokens(monkeypatch):
    repo_root = Path.cwd()
    image_path = repo_root / ".multimodal_test_image.png"
    image_path.write_bytes(b"fakepngdata")

    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test-key")

    attachments_module = importlib.import_module("core.attachments")
    message, warnings = attachments_module.build_user_message(f"Please inspect @{image_path.name}")

    try:
        assert not warnings
        assert message["role"] == "user"
        assert isinstance(message["content"], list)
        assert message["content"][0]["type"] == "text"
        assert f"[Attached path: {image_path.name}]" in message["content"][0]["text"]
        assert message["content"][1]["type"] == "attachment"
        assert message["content"][1]["attachment"]["filename"] == image_path.name
    finally:
        if image_path.exists():
            image_path.unlink()


def test_build_user_message_includes_pdf_path(monkeypatch):
    repo_root = Path.cwd()
    pdf_path = repo_root / ".multimodal_test_pdf.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test-key")

    attachments_module = importlib.import_module("core.attachments")
    message, warnings = attachments_module.build_user_message(f"Summarize @{pdf_path.name}")

    try:
        assert not warnings
        assert isinstance(message["content"], list)
        assert message["content"][0]["type"] == "text"
        assert f"[Attached path: {pdf_path.name}]" in message["content"][0]["text"]
        assert message["content"][1]["type"] == "attachment"
        assert message["content"][1]["attachment"]["filename"] == pdf_path.name
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def test_build_user_message_parses_unquoted_pdf_path_with_spaces(monkeypatch):
    repo_root = Path.cwd()
    pdf_path = repo_root / ".multimodal spaced report.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test-key")

    attachments_module = importlib.import_module("core.attachments")
    message, warnings = attachments_module.build_user_message(f"@{pdf_path.name} summarize this pdf")

    try:
        assert not warnings
        assert isinstance(message["content"], list)
        assert "summarize this pdf" in message["content"][0]["text"]
        assert f"[Attached path: {pdf_path.name}]" in message["content"][0]["text"]
        assert message["content"][1]["type"] == "attachment"
        assert message["content"][1]["attachment"]["filename"] == pdf_path.name
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def test_build_user_message_parses_unquoted_image_path_with_spaces(monkeypatch):
    repo_root = Path.cwd()
    image_path = repo_root / ".multimodal spaced photo.png"
    image_path.write_bytes(b"fakepngdata")

    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test-key")

    attachments_module = importlib.import_module("core.attachments")
    message, warnings = attachments_module.build_user_message(f"check @{image_path.name} please")

    try:
        assert not warnings
        assert isinstance(message["content"], list)
        assert "check please" in message["content"][0]["text"]
        assert f"[Attached path: {image_path.name}]" in message["content"][0]["text"]
        assert message["content"][1]["type"] == "attachment"
        assert message["content"][1]["attachment"]["filename"] == image_path.name
    finally:
        if image_path.exists():
            image_path.unlink()


def test_build_user_message_keeps_unsupported_file_path_as_text():
    attachments_module = importlib.import_module("core.attachments")

    message, warnings = attachments_module.build_user_message("Inspect @core/tools.py")

    assert warnings == []
    assert message == {"role": "user", "content": "Inspect core/tools.py"}


def test_build_user_message_supports_attach_command_equivalent(monkeypatch):
    repo_root = Path.cwd()
    pdf_path = repo_root / ".multimodal attach command.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "test-key")

    attachments_module = importlib.import_module("core.attachments")
    rewritten = rewrite_attach_command(f"/attach {pdf_path.name} summarize this pdf")
    message, warnings = attachments_module.build_user_message(rewritten or "")

    try:
        assert not warnings
        assert isinstance(message["content"], list)
        assert "summarize this pdf" in message["content"][0]["text"]
        assert f"[Attached path: {pdf_path.name}]" in message["content"][0]["text"]
        assert message["content"][1]["type"] == "attachment"
        assert message["content"][1]["attachment"]["filename"] == pdf_path.name
    finally:
        if pdf_path.exists():
            pdf_path.unlink()


def test_attachment_suggestions_include_matching_workspace_entries(tmp_path, monkeypatch):
    workspace_file = Path.cwd() / ".multimodal_suggestion_demo.pdf"
    workspace_file.write_bytes(b"%PDF-1.4\n")

    try:
        query = get_attachment_query_at_cursor("Please inspect @.multimodal_sug")
        assert query == ".multimodal_sug"

        suggestions = get_attachment_suggestions(query or "")
        assert any(item["value"] == workspace_file.name for item in suggestions)
    finally:
        if workspace_file.exists():
            workspace_file.unlink()


def test_apply_attachment_suggestion_replaces_partial_token():
    updated = apply_attachment_suggestion("Check @docs/rep", "docs/report.pdf")

    assert updated == "Check @docs/report.pdf "


def test_apply_attachment_parent_navigation_moves_to_parent_directory():
    updated = apply_attachment_parent_navigation("Check @docs/guides/")

    assert updated == "Check @docs/"


def test_attachment_suggestions_support_fuzzy_matching():
    workspace_file = Path.cwd() / ".multimodal_fuzzy_report.pdf"
    workspace_file.write_bytes(b"%PDF-1.4\n")

    try:
        suggestions = get_attachment_suggestions("fzrpt")
        assert any(item["value"] == workspace_file.name for item in suggestions)
    finally:
        if workspace_file.exists():
            workspace_file.unlink()


def test_attachment_suggestions_fall_back_to_global_workspace_search():
    suggestions = get_attachment_suggestions("llm_ut")

    assert any(item["value"] == "llm_client/llm_utils.py" for item in suggestions)


def test_attachment_suggestions_do_not_expand_nested_directories_from_global_fallback():
    base_dir = Path.cwd() / ".multimodal_expand_root"
    nested_dir = base_dir / "child"
    nested_dir.mkdir(parents=True, exist_ok=True)

    try:
        suggestions = get_attachment_suggestions(".multimodal_expand", limit=16)
        values = {item["value"] for item in suggestions}

        assert f"{base_dir.name}/" in values
        assert f"{base_dir.name}/child/" not in values
    finally:
        if nested_dir.exists():
            nested_dir.rmdir()
        if base_dir.exists():
            base_dir.rmdir()


def test_attachment_suggestions_for_explicit_directory_do_not_mix_global_results():
    base_dir = Path.cwd() / ".multimodal_nav_dir"
    nested_file = base_dir / "inside.pdf"
    base_dir.mkdir(exist_ok=True)
    nested_file.write_bytes(b"%PDF-1.4\n")

    try:
        suggestions = get_attachment_suggestions(f"{base_dir.name}/")
        assert suggestions
        assert any(item["value"] == f"{base_dir.name}/inside.pdf" for item in suggestions)
        assert all(item["value"].startswith(f"{base_dir.name}/") for item in suggestions)
    finally:
        if nested_file.exists():
            nested_file.unlink()
        if base_dir.exists():
            base_dir.rmdir()


def test_attachment_suggestions_for_scoped_directory_prefix_do_not_mix_global_results():
    base_dir = Path.cwd() / ".multimodal_scope_dir"
    nested_image = base_dir / "main-ui.png"
    base_dir.mkdir(exist_ok=True)
    nested_image.write_bytes(b"fakepngdata")

    try:
        suggestions = get_attachment_suggestions(f"{base_dir.name}/main", limit=16)
        values = {item["value"] for item in suggestions}

        assert f"{base_dir.name}/main-ui.png" in values
        assert all(value.startswith(f"{base_dir.name}/") for value in values)
    finally:
        if nested_image.exists():
            nested_image.unlink()
        if base_dir.exists():
            base_dir.rmdir()


def test_attachment_suggestion_labels_distinguish_dir_pdf_image_and_file():
    base_dir = Path.cwd() / ".multimodal_label_dir"
    pdf_path = Path.cwd() / ".multimodal_label_report.pdf"
    image_path = Path.cwd() / ".multimodal_label_photo.png"
    text_path = Path.cwd() / ".multimodal_label_notes.txt"

    base_dir.mkdir(exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4\n")
    image_path.write_bytes(b"fakepngdata")
    text_path.write_text("hello", encoding="utf-8")

    try:
        suggestions = get_attachment_suggestions(".multimodal_label", limit=16)
        labels = {item["value"]: item["label"] for item in suggestions}

        assert labels[base_dir.name + "/"].startswith("DIR  ")
        assert labels[pdf_path.name].startswith("PDF  ")
        assert labels[image_path.name].startswith("IMG  ")
        assert labels[text_path.name].startswith("FILE ")
    finally:
        if pdf_path.exists():
            pdf_path.unlink()
        if image_path.exists():
            image_path.unlink()
        if text_path.exists():
            text_path.unlink()
        if base_dir.exists():
            base_dir.rmdir()


def test_is_browser_openable_path_supports_images_and_pdfs_only():
    assert _is_browser_openable_path(Path("docs/report.pdf")) is True
    assert _is_browser_openable_path(Path("images/photo.png")) is True
    assert _is_browser_openable_path(Path("images/photo.jpg")) is True
    assert _is_browser_openable_path(Path("images/vector.svg")) is True
    assert _is_browser_openable_path(Path("core/tools.py")) is False
    assert _is_browser_openable_path(Path("README.md")) is False