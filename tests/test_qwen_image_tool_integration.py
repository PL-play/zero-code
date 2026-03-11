from __future__ import annotations

import os
from base64 import b64decode
from pathlib import Path

import pytest
from dotenv import load_dotenv

from llm_client.qwen_image import (
    edit_image_with_qwen,
    generate_image_with_qwen,
    qwen_image_config_from_env,
    qwen_image_edit_config_from_env,
)

load_dotenv(override=False)


def _integration_config():
    return qwen_image_config_from_env(os.environ)


def _integration_edit_config():
    return qwen_image_edit_config_from_env(os.environ)


def _write_sample_png(path: Path) -> None:
    path.write_bytes(
        b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wn2S2sAAAAASUVORK5CYII="
        )
    )


def _skip_for_ssl_issue(exc: RuntimeError) -> None:
    message = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in message or "unable to get local issuer certificate" in message:
        pytest.skip(f"Outbound SSL verification is blocked by the local environment: {message}")
    if "InvalidParameter" in message and "broken data stream when reading image file" in message:
        pytest.skip(f"DashScope rejected the tiny integration fixture image: {message}")


@pytest.mark.integration
def test_qwen_image_real_generation(tmp_path):
    cfg = _integration_config()
    if cfg is None:
        pytest.skip("Missing DASHSCOPE_API_KEY / DASHSCOPE_IMAGE_MODEL for real image generation test.")

    try:
        result = generate_image_with_qwen(
            cfg,
            prompt="一只坐在木桌上的橘猫，窗边自然光，写实摄影风格。",
            output_dir=tmp_path,
            size=cfg.default_size or "1024*1024",
            filename_prefix="integration-cat",
            workspace_root=tmp_path,
        )
    except RuntimeError as exc:
        _skip_for_ssl_issue(exc)
        raise

    assert result["provider"] == "dashscope"
    assert result["model"] == cfg.model
    assert result["images"], "Expected at least one generated image"

    image_info = result["images"][0]
    saved_path = tmp_path / image_info["path"]
    assert saved_path.exists(), f"Generated image was not saved: {saved_path}"
    assert saved_path.stat().st_size > 0

    usage = result.get("usage") or {}
    assert usage.get("image_count", 0) >= 1


@pytest.mark.integration
def test_qwen_image_real_edit(tmp_path):
    cfg = _integration_edit_config()
    if cfg is None:
        pytest.skip("Missing DASHSCOPE_API_KEY / DASHSCOPE_IMAGE_EDIT_MODEL for real image edit test.")

    source_image = tmp_path / "source.png"
    _write_sample_png(source_image)

    try:
        result = edit_image_with_qwen(
            cfg,
            prompt="保持主体不变，把背景改成明亮的极简工作室，产品摄影风格。",
            image_paths=[source_image],
            output_dir=tmp_path,
            size=cfg.default_size or "1024*1024",
            filename_prefix="integration-edit",
            workspace_root=tmp_path,
        )
    except RuntimeError as exc:
        _skip_for_ssl_issue(exc)
        raise

    assert result["provider"] == "dashscope"
    assert result["model"] == cfg.model
    assert result["images"], "Expected at least one edited image"

    image_info = result["images"][0]
    saved_path = tmp_path / image_info["path"]
    assert saved_path.exists(), f"Edited image was not saved: {saved_path}"
    assert saved_path.stat().st_size > 0

    usage = result.get("usage") or {}
    assert usage.get("image_count", 0) >= 1