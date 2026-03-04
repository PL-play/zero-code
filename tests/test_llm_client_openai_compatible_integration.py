import os

import pytest
from dotenv import load_dotenv

from llm_client.interface import LLMRequest, OpenAICompatibleChatConfig
from llm_client.llm_factory import OpenAICompatibleChatLLMService

load_dotenv(override=False)


SYSTEM_PROMPT = (
    "你是一个测试助手。回答保持简洁准确；"
    "对于算数问题，总是调用工具,禁止自己回答"
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算两个整数的和",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "第一个整数"},
                    "b": {"type": "integer", "description": "第二个整数"},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        },
    }
]


def _test_config() -> OpenAICompatibleChatConfig | None:
    model = os.getenv("OPENAI_COMPAT_MODEL")
    base_url = os.getenv("OPENAI_COMPAT_BASE_URL")
    api_key = os.getenv("OPENAI_COMPAT_API_KEY")

    if not (model and base_url and api_key):
        return None

    return OpenAICompatibleChatConfig(
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_s=120,
        max_tokens=128,
        temperature=0,
        max_retries=1,
    )


@pytest.mark.integration
@pytest.mark.anyio
async def test_openai_compatible_client_real_stream(capsys):
    cfg = _test_config()
    if cfg is None:
        pytest.skip(
            "Missing OPENAI_COMPAT_MODEL / OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY for real-call test."
        )

    client = OpenAICompatibleChatLLMService(cfg)
    request = LLMRequest(
        messages=[{"role": "user", "content": "2+2等于几？"}],
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0,
    )

    chunks: list[str] = []
    think_chunks: list[str] = []
    tool_calls_last: list[dict] = []
    final_usage = None
    saw_final = False
    try:
        async for chunk in client.stream(request):
            if chunk.think:
                think_chunks.append(chunk.think)

            if chunk.delta_text:
                with capsys.disabled():
                    print(chunk.delta_text, end="", flush=True)
                chunks.append(chunk.delta_text)

            if chunk.tool_calls:
                tool_calls_last = chunk.tool_calls

            if chunk.token_usage is not None:
                final_usage = chunk.token_usage.as_dict()

            if chunk.is_final:
                saw_final = True

        full_text = "".join(chunks).strip()
        full_think = "".join(think_chunks).strip()
        usage_payload = client.get_last_token_usage() or {}
        with capsys.disabled():
            print()
            print("[FULL_TEXT]", full_text)
            print("[FULL_THINK]", full_think)
            print("[TOOL_CALLS]", tool_calls_last)
            print("[FINAL_CHUNK_USAGE]", final_usage)
            if not full_think:
                print(
                    "[THINK_HINT] provider did not expose reasoning text. "
                    f"reasoning_tokens={usage_payload.get('reasoning_tokens')}"
                )

        print("usage:")
        print(usage_payload)

        assert saw_final, "Did not receive final stream chunk"
        assert tool_calls_last, "Expected streamed tool call, but none was captured"
        assert any((call.get("function") or {}).get("name") == "calculator" for call in tool_calls_last), (
            "Expected calculator tool call in streamed tool_calls"
        )
    finally:
        await client.close()


@pytest.mark.integration
@pytest.mark.anyio
async def test_openai_compatible_client_real_complete(capsys):
    cfg = _test_config()
    if cfg is None:
        pytest.skip(
            "Missing OPENAI_COMPAT_MODEL / OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY for real-call test."
        )

    client = OpenAICompatibleChatLLMService(cfg)
    request = LLMRequest(
        messages=[{"role": "user", "content": "5+7等于几？"}],
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0,
    )

    try:
        response = await client.complete(request)

        with capsys.disabled():
            print("\n")
            print("=== COMPLETE RUN ===")
            print("[FULL_TEXT]", response.raw_text)
            print("[FULL_THINK]", response.think)
            print("[TOOL_CALLS]", response.tool_calls)
            print("[TOKEN_USAGE]", response.token_usage.as_dict() if response.token_usage else None)
            print(f"[STREAM_CHUNKS_COUNT] {len(response.stream_chunks)}")

        assert response.tool_calls, "Expected tool calls in complete() response"
        assert any((call.get("function") or {}).get("name") == "calculator" for call in response.tool_calls), (
            "Expected calculator tool call in complete() tool_calls"
        )
        assert len(response.stream_chunks) > 0, "Expected stream_chunks to be aggregated in complete() response"
    finally:
        await client.close()


@pytest.mark.integration
@pytest.mark.anyio
async def test_openai_compatible_client_real_complete_hooks(capsys):
    cfg = _test_config()
    if cfg is None:
        pytest.skip(
            "Missing OPENAI_COMPAT_MODEL / OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY for real-call test."
        )

    client = OpenAICompatibleChatLLMService(cfg)
    request = LLMRequest(
        messages=[{"role": "user", "content": "写一首诗歌。表达爱情，13行左右"}],
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_choice="auto",
        max_tokens=256,
        temperature=0.4,
    )

    think_chunks_captured = []
    text_chunks_captured = []
    end_hook_calls = []

    def on_chunk_think(think_str: str):
        if think_str:
            think_chunks_captured.append(think_str)
            with capsys.disabled():
                # 蓝色打印 think 内容
                print(f"\033[94m{think_str}\033[0m", end="", flush=True)

    def on_chunk_delta_text(text_str: str):
        if text_str:
            text_chunks_captured.append(text_str)
            with capsys.disabled():
                print(text_str, end="", flush=True)

    def on_stream_end(res):
        end_hook_calls.append(res)
        with capsys.disabled():
            print(f"\n[STREAM_END_HOOK] tokens: {res.token_usage.total_tokens}")

    try:
        with capsys.disabled():
            print("\n=== COMPLETE WITH HOOKS RUN ===")
        response = await client.complete(
            request,
            on_chunk_think=on_chunk_think,
            on_chunk_delta_text=on_chunk_delta_text,
            on_stream_end=on_stream_end,
        )

    finally:
        await client.close()
