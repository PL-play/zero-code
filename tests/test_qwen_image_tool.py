from __future__ import annotations

from pathlib import Path

from llm_client.qwen_image import (
    QwenImageError,
    QwenImageEditConfig,
    QwenImageConfig,
    build_qwen_image_edit_payload,
    build_qwen_image_payload,
    edit_image_with_qwen,
    generate_image_with_qwen,
    qwen_image_edit_config_from_env,
    qwen_image_config_from_env,
    summarize_image_operation_error,
    summarize_image_operation_result,
)


def test_qwen_image_config_from_env_requires_model_and_api_key():
    assert qwen_image_config_from_env({}) is None
    assert qwen_image_config_from_env({"DASHSCOPE_API_KEY": "sk-test"}) is None
    assert qwen_image_config_from_env({"DASHSCOPE_IMAGE_MODEL": "qwen-image-2.0-pro"}) is None


def test_qwen_image_config_from_env_parses_optional_settings():
    cfg = qwen_image_config_from_env(
        {
            "DASHSCOPE_API_KEY": "sk-test",
            "DASHSCOPE_IMAGE_MODEL": "qwen-image-2.0-pro",
            "DASHSCOPE_IMAGE_BASE_URL": "https://dashscope-intl.aliyuncs.com/api/v1",
            "DASHSCOPE_IMAGE_DEFAULT_SIZE": "1024*1024",
            "DASHSCOPE_IMAGE_OUTPUT_DIR": "outputs/custom-images",
            "DASHSCOPE_IMAGE_PROMPT_EXTEND": "false",
            "DASHSCOPE_IMAGE_WATERMARK": "1",
            "DASHSCOPE_IMAGE_USE_PROXY": "true",
            "DASHSCOPE_IMAGE_TIMEOUT_S": "45",
        }
    )

    assert cfg is not None
    assert cfg.model == "qwen-image-2.0-pro"
    assert cfg.base_url == "https://dashscope-intl.aliyuncs.com/api/v1"
    assert cfg.default_size == "1024*1024"
    assert cfg.output_dir == "outputs/custom-images"
    assert cfg.prompt_extend is False
    assert cfg.watermark is True
    assert cfg.use_proxy is True
    assert cfg.timeout_s == 45.0


def test_qwen_image_config_from_env_defaults_to_no_proxy():
    cfg = qwen_image_config_from_env(
        {
            "DASHSCOPE_API_KEY": "sk-test",
            "DASHSCOPE_IMAGE_MODEL": "qwen-image-2.0-pro",
        }
    )

    assert cfg is not None
    assert cfg.use_proxy is False


def test_qwen_image_edit_config_from_env_requires_model_and_api_key():
    assert qwen_image_edit_config_from_env({}) is None
    assert qwen_image_edit_config_from_env({"DASHSCOPE_API_KEY": "sk-test"}) is None
    assert qwen_image_edit_config_from_env({"DASHSCOPE_IMAGE_EDIT_MODEL": "qwen-image-2.0-pro"}) is None


def test_qwen_image_edit_config_from_env_parses_optional_settings():
    cfg = qwen_image_edit_config_from_env(
        {
            "DASHSCOPE_API_KEY": "sk-test",
            "DASHSCOPE_IMAGE_EDIT_MODEL": "qwen-image-2.0-pro",
            "DASHSCOPE_IMAGE_EDIT_BASE_URL": "https://dashscope-intl.aliyuncs.com/api/v1",
            "DASHSCOPE_IMAGE_EDIT_DEFAULT_SIZE": "1536*1024",
            "DASHSCOPE_IMAGE_EDIT_OUTPUT_DIR": "outputs/custom-edits",
            "DASHSCOPE_IMAGE_EDIT_PROMPT_EXTEND": "0",
            "DASHSCOPE_IMAGE_EDIT_WATERMARK": "true",
            "DASHSCOPE_IMAGE_EDIT_USE_PROXY": "1",
            "DASHSCOPE_IMAGE_EDIT_TIMEOUT_S": "75",
        }
    )

    assert cfg is not None
    assert cfg.model == "qwen-image-2.0-pro"
    assert cfg.base_url == "https://dashscope-intl.aliyuncs.com/api/v1"
    assert cfg.default_size == "1536*1024"
    assert cfg.output_dir == "outputs/custom-edits"
    assert cfg.prompt_extend is False
    assert cfg.watermark is True
    assert cfg.use_proxy is True
    assert cfg.timeout_s == 75.0


def test_qwen_image_edit_config_from_env_defaults_to_no_proxy():
    cfg = qwen_image_edit_config_from_env(
        {
            "DASHSCOPE_API_KEY": "sk-test",
            "DASHSCOPE_IMAGE_EDIT_MODEL": "qwen-image-2.0-pro",
        }
    )

    assert cfg is not None
    assert cfg.use_proxy is False


def test_build_qwen_image_payload_matches_sync_api_shape():
    payload = build_qwen_image_payload(
        model="qwen-image-2.0-pro",
        prompt="画一只猫",
        negative_prompt="模糊",
        size="1024*1024",
        prompt_extend=False,
        watermark=True,
    )

    assert payload == {
        "model": "qwen-image-2.0-pro",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": "画一只猫"}],
                }
            ]
        },
        "parameters": {
            "prompt_extend": False,
            "watermark": True,
            "negative_prompt": "模糊",
            "size": "1024*1024",
        },
    }


def test_build_qwen_image_edit_payload_matches_sync_api_shape():
    payload = build_qwen_image_edit_payload(
        model="qwen-image-2.0-pro",
        prompt="把背景改成海边日落",
        image_sources=["data:image/png;base64,AAA", "https://example.com/input.png"],
        negative_prompt="模糊",
        size="1024*1024",
        n=2,
        prompt_extend=False,
        watermark=True,
    )

    assert payload == {
        "model": "qwen-image-2.0-pro",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": "data:image/png;base64,AAA"},
                        {"image": "https://example.com/input.png"},
                        {"text": "把背景改成海边日落"},
                    ],
                }
            ]
        },
        "parameters": {
            "prompt_extend": False,
            "watermark": True,
            "negative_prompt": "模糊",
            "size": "1024*1024",
            "n": 2,
        },
    }


def test_generate_image_with_qwen_downloads_and_persists_files(monkeypatch, tmp_path):
    config = QwenImageConfig(api_key="sk-test", model="qwen-image-2.0-pro")

    def fake_post_json(url, api_key, payload, timeout_s, **kwargs):
        assert url.endswith("/services/aigc/multimodal-generation/generation")
        assert api_key == "sk-test"
        assert payload["model"] == "qwen-image-2.0-pro"
        return {
            "request_id": "req-123",
            "output": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"image": "https://example.com/generated/demo.png?Expires=1"}
                            ],
                        }
                    }
                ]
            },
            "usage": {"image_count": 1, "width": 1024, "height": 1024},
        }

    monkeypatch.setattr("llm_client.qwen_image._post_json", fake_post_json)
    monkeypatch.setattr("llm_client.qwen_image._download_binary", lambda url, timeout_s, **kwargs: b"png-bytes")

    output_dir = tmp_path / "generated"
    result = generate_image_with_qwen(
        config,
        prompt="画一只猫",
        output_dir=output_dir,
        filename_prefix="cat-art",
        workspace_root=tmp_path,
    )

    assert result["provider"] == "dashscope"
    assert result["model"] == "qwen-image-2.0-pro"
    assert result["width"] == 1024
    assert result["height"] == 1024
    assert result["images"][0]["path"].startswith("generated/")
    assert result["images"][0]["source_url"].startswith("https://example.com/generated/demo.png")

    saved_path = tmp_path / result["images"][0]["path"]
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"png-bytes"


def test_edit_image_with_qwen_downloads_and_persists_files(monkeypatch, tmp_path):
    config = QwenImageEditConfig(api_key="sk-test", model="qwen-image-2.0-pro")

    source_image = tmp_path / "source.png"
    source_image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc``\x00\x00\x00\x04\x00\x01\x0b\x0e-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def fake_post_json(url, api_key, payload, timeout_s, **kwargs):
        assert url.endswith("/services/aigc/multimodal-generation/generation")
        assert api_key == "sk-test"
        assert payload["model"] == "qwen-image-2.0-pro"
        content = payload["input"]["messages"][0]["content"]
        assert content[0]["image"].startswith("data:image/png;base64,")
        assert content[1]["text"] == "把猫改成宇航员"
        assert payload["parameters"]["n"] == 2
        return {
            "request_id": "req-edit-123",
            "output": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"image": "https://example.com/edited/demo.png?Expires=1"}
                            ],
                        }
                    }
                ]
            },
            "usage": {"image_count": 1, "width": 1024, "height": 1024},
        }

    monkeypatch.setattr("llm_client.qwen_image._post_json", fake_post_json)
    monkeypatch.setattr("llm_client.qwen_image._download_binary", lambda url, timeout_s, **kwargs: b"edited-png-bytes")

    output_dir = tmp_path / "edited"
    result = edit_image_with_qwen(
        config,
        prompt="把猫改成宇航员",
        image_paths=[source_image],
        output_dir=output_dir,
        n=2,
        filename_prefix="cat-edit",
        workspace_root=tmp_path,
    )

    assert result["provider"] == "dashscope"
    assert result["model"] == "qwen-image-2.0-pro"
    assert result["width"] == 1024
    assert result["height"] == 1024
    assert result["images"][0]["path"].startswith("edited/")
    assert result["images"][0]["source_url"].startswith("https://example.com/edited/demo.png")

    saved_path = tmp_path / result["images"][0]["path"]
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"edited-png-bytes"


def test_summarize_image_operation_result_returns_compact_agent_friendly_fields():
    summary = summarize_image_operation_result(
        {
            "provider": "dashscope",
            "model": "qwen-image-2.0-pro",
            "request_id": "req-compact-1",
            "images": [
                {
                    "path": "outputs/generated-images/cat-1.png",
                    "source_url": "https://example.com/cat-1.png?Expires=1",
                },
                {
                    "path": "outputs/generated-images/cat-2.png",
                    "source_url": "https://example.com/cat-2.png?Expires=1",
                },
            ],
            "usage": {"image_count": 2, "width": 1024, "height": 1024},
            "width": 1024,
            "height": 1024,
            "raw_response": {"large": "payload"},
        },
        operation="edit_image",
        input_paths=["assets/source.png"],
    )

    assert summary == {
        "ok": True,
        "operation": "edit_image",
        "provider": "dashscope",
        "model": "qwen-image-2.0-pro",
        "request_id": "req-compact-1",
        "image_count": 2,
        "paths": [
            "outputs/generated-images/cat-1.png",
            "outputs/generated-images/cat-2.png",
        ],
        "images": [
            {
                "path": "outputs/generated-images/cat-1.png",
                "source_url": "https://example.com/cat-1.png?Expires=1",
            },
            {
                "path": "outputs/generated-images/cat-2.png",
                "source_url": "https://example.com/cat-2.png?Expires=1",
            },
        ],
        "primary_path": "outputs/generated-images/cat-1.png",
        "input_paths": ["assets/source.png"],
        "width": 1024,
        "height": 1024,
        "usage": {"image_count": 2, "width": 1024, "height": 1024},
    }


def test_summarize_image_operation_error_keeps_category_and_retryability():
    summary = summarize_image_operation_error(
        QwenImageError(
            "DashScope image request failed with HTTP 429: rate limited",
            category="provider_http_error",
            retryable=True,
            status_code=429,
        ),
        operation="generate_image",
    )

    assert summary == {
        "ok": False,
        "operation": "generate_image",
        "error": {
            "category": "provider_http_error",
            "message": "DashScope image request failed with HTTP 429: rate limited",
            "retryable": True,
            "status_code": 429,
        },
    }


def test_summarize_image_operation_error_maps_plain_value_error_to_invalid_input():
    summary = summarize_image_operation_error(
        ValueError("size must be 1024*1024"),
        operation="edit_image",
        input_paths=["assets/source.png"],
    )

    assert summary == {
        "ok": False,
        "operation": "edit_image",
        "error": {
            "category": "invalid_input",
            "message": "size must be 1024*1024",
            "retryable": False,
        },
        "input_paths": ["assets/source.png"],
    }