"""
LLM factory utilities for zrag.

This module provides a small, explicit way to build an OpenAI-compatible `LLMService`
from environment / `src.config.settings`, similar in spirit to agent-psychology's
model utilities, but adapted to zrag's `LLMService` Protocol.
"""

from __future__ import annotations

import inspect
import logging
import asyncio
import random
from typing import cast, Callable
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from openai import AsyncOpenAI

from .interface import LLMService, OpenAICompatibleChatConfig, LLMTokenUsage, LLMRequest, LLMResponse, \
    LLMStreamChunk
from .llm_utils import parse_json_from_model_output_detailed

try:
    from openai.types.chat import ChatCompletion, ChatCompletionMessage, \
        ChatCompletionChunk  # type: ignore[import-not-found]
except Exception as e:  # pragma: no cover
    # Runtime will still work because we treat these as typing-only. Keep minimal fallback.
    ChatCompletion = Any  # type: ignore[assignment]
    ChatCompletionMessage = Any  # type: ignore[assignment]

logger = logging.getLogger(__name__)




class OpenAICompatibleChatLLMService(LLMService):
    """
    Minimal OpenAI-compatible chat completion client.

    - Uses `openai.AsyncOpenAI`
    - Records token usage from response.usage (if present)
    """

    def __init__(self, cfg: OpenAICompatibleChatConfig):
        self._cfg = cfg
        self._client: Any = None
        self._last_usage: Dict[str, Any] = {}

        logger.info(
            "GraphExtractor LLM: base_url=%s model=%s timeout_s=%s max_tokens=%s temperature=%s api_key=%s",
            cfg.base_url,
            cfg.model,
            cfg.timeout_s,
            cfg.max_tokens,
            cfg.temperature,
            "set" if bool(cfg.api_key) else "missing",
        )

    def get_last_token_usage(self) -> Dict[str, Any]:
        return dict(self._last_usage or {})

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import AsyncOpenAI  # type: ignore

        self._client = AsyncOpenAI(
            base_url=self._cfg.base_url,
            api_key=self._cfg.api_key,
            timeout=self._cfg.timeout_s,
        )
        return self._client

    async def close(self) -> None:
        """
        Close underlying HTTP resources (httpx) to avoid ResourceWarning in tests.
        """
        if self._client is None:
            return
        try:
            # openai.AsyncOpenAI exposes `close()` (async).
            await self._client.close()
        finally:
            self._client = None

    def _record_usage(self, usage: Any, *, method: str) -> None:
        self._last_usage = self._usage_dict_from_any(usage)

        total_tokens = self._last_usage.get("total_tokens")
        if isinstance(total_tokens, int) and total_tokens > 0:
            logger.info(
                "[llm:%s] token_usage prompt=%s completion=%s total=%s",
                method,
                self._last_usage.get("prompt_tokens"),
                self._last_usage.get("completion_tokens"),
                self._last_usage.get("total_tokens"),
            )

    def _usage_dict_from_any(self, usage: Any) -> Dict[str, Any]:
        """
        Normalize token usage from various provider shapes.
        Supports:
        - OpenAI usage: prompt_tokens/completion_tokens/total_tokens
        - Some providers: input_tokens/output_tokens/total_tokens
        - Dict payloads (from model_dump / raw json)
        """
        if usage is None:
            return {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
            }

        raw: Dict[str, Any] = {}

        # dict-like
        if isinstance(usage, dict):
            raw = dict(usage)
        else:
            # object-like: prefer model_dump, then __dict__
            if hasattr(usage, "model_dump"):
                try:
                    dumped = usage.model_dump()
                    if isinstance(dumped, dict):
                        raw = dict(dumped)
                except Exception:
                    raw = {}
            if not raw:
                try:
                    raw = dict(usage.__dict__)
                except Exception:
                    raw = {}

        def _safe_int_or_none(v: Any) -> Optional[int]:
            if v is None:
                return None
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                try:
                    return int(float(s))
                except Exception:
                    return None
            return None

        def _pick_int(payload: Dict[str, Any], keys: List[str]) -> Optional[int]:
            for key in keys:
                if key in payload:
                    iv = _safe_int_or_none(payload.get(key))
                    if iv is not None:
                        return iv
            return None

        def _as_dict(v: Any) -> Dict[str, Any]:
            if isinstance(v, dict):
                return v
            if hasattr(v, "model_dump"):
                try:
                    dumped = v.model_dump()
                    if isinstance(dumped, dict):
                        return dumped
                except Exception:
                    return {}
            return {}

        prompt = _pick_int(raw, [
            "prompt_tokens",
            "input_tokens",
            "prompt_token_count",
            "promptTokens",
            "inputTokenCount",
            "prompt_count",
        ])
        completion = _pick_int(raw, [
            "completion_tokens",
            "output_tokens",
            "completion_token_count",
            "completionTokens",
            "outputTokenCount",
            "generated_tokens",
            "candidates_token_count",
        ])
        total = _pick_int(raw, [
            "total_tokens",
            "total_token_count",
            "totalTokens",
            "token_count",
            "usage_tokens",
        ])

        if total is None and prompt is not None and completion is not None:
            total = prompt + completion

        prompt_details = _as_dict(raw.get("prompt_tokens_details"))
        completion_details = _as_dict(raw.get("completion_tokens_details"))
        output_details = _as_dict(raw.get("output_tokens_details"))
        input_details = _as_dict(raw.get("input_tokens_details"))

        prompt_audio_tokens = _pick_int(prompt_details, ["audio_tokens", "audioTokenCount"])
        if prompt_audio_tokens is None:
            prompt_audio_tokens = _pick_int(input_details, ["audio_tokens", "audioTokenCount"])
        prompt_cached_tokens = _pick_int(
            raw,
            ["prompt_cache_hit_tokens", "cached_tokens", "cache_hit_tokens", "prompt_cached_tokens"],
        )
        if prompt_cached_tokens is None:
            prompt_cached_tokens = _pick_int(prompt_details, ["cached_tokens", "cache_hit_tokens", "cacheHitTokens"])
        prompt_cache_miss_tokens = _pick_int(raw, ["prompt_cache_miss_tokens", "cache_miss_tokens", "cacheMissTokens"])
        if prompt_cache_miss_tokens is None:
            prompt_cache_miss_tokens = _pick_int(prompt_details, ["cache_miss_tokens", "cacheMissTokens"])
        prompt_text_tokens = _pick_int(prompt_details, ["text_tokens", "textTokenCount"])
        if prompt_text_tokens is None:
            prompt_text_tokens = _pick_int(input_details, ["text_tokens", "textTokenCount"])
        prompt_image_tokens = _pick_int(prompt_details, ["image_tokens", "imageTokenCount"])
        if prompt_image_tokens is None:
            prompt_image_tokens = _pick_int(input_details, ["image_tokens", "imageTokenCount"])

        completion_reasoning_tokens = _pick_int(
            completion_details,
            ["reasoning_tokens", "reasoningTokenCount", "reasoning_token", "reasoningTokens", "thinking_tokens"],
        )
        if completion_reasoning_tokens is None:
            completion_reasoning_tokens = _pick_int(
                output_details,
                ["reasoning_tokens", "reasoningTokenCount", "reasoning_token", "reasoningTokens", "thinking_tokens"],
            )
        completion_audio_tokens = _pick_int(completion_details, ["audio_tokens", "audioTokenCount"])
        if completion_audio_tokens is None:
            completion_audio_tokens = _pick_int(output_details, ["audio_tokens", "audioTokenCount"])
        completion_text_tokens = _pick_int(completion_details, ["text_tokens", "textTokenCount"])
        if completion_text_tokens is None:
            completion_text_tokens = _pick_int(output_details, ["text_tokens", "textTokenCount"])
        completion_image_tokens = _pick_int(completion_details, ["image_tokens", "imageTokenCount"])
        if completion_image_tokens is None:
            completion_image_tokens = _pick_int(output_details, ["image_tokens", "imageTokenCount"])
        accepted_prediction_tokens = _pick_int(
            completion_details,
            ["accepted_prediction_tokens", "acceptedPredictionTokens"],
        )
        if accepted_prediction_tokens is None:
            accepted_prediction_tokens = _pick_int(
                output_details,
                ["accepted_prediction_tokens", "acceptedPredictionTokens"],
            )
        rejected_prediction_tokens = _pick_int(
            completion_details,
            ["rejected_prediction_tokens", "rejectedPredictionTokens"],
        )
        if rejected_prediction_tokens is None:
            rejected_prediction_tokens = _pick_int(
                output_details,
                ["rejected_prediction_tokens", "rejectedPredictionTokens"],
            )

        # Provider-agnostic aliases (keep None when unknown)
        cached_tokens = _pick_int(raw, ["cached_tokens", "cache_hit_tokens", "prompt_cache_hit_tokens"])
        if cached_tokens is None:
            cached_tokens = prompt_cached_tokens
        cache_hit_tokens = _pick_int(raw, ["cache_hit_tokens", "prompt_cache_hit_tokens", "cached_tokens"])
        if cache_hit_tokens is None:
            cache_hit_tokens = prompt_cached_tokens
        cache_miss_tokens = _pick_int(raw, ["cache_miss_tokens", "prompt_cache_miss_tokens"])
        if cache_miss_tokens is None:
            cache_miss_tokens = prompt_cache_miss_tokens

        reasoning_tokens = _pick_int(
            raw,
            ["reasoning_tokens", "reasoning_token", "reasoningTokens", "thinking_tokens"],
        )
        if reasoning_tokens is None:
            reasoning_tokens = completion_reasoning_tokens

        accepted_tokens = _pick_int(raw, ["accepted_prediction_tokens"])
        if accepted_tokens is None:
            accepted_tokens = accepted_prediction_tokens
        rejected_tokens = _pick_int(raw, ["rejected_prediction_tokens"])
        if rejected_tokens is None:
            rejected_tokens = rejected_prediction_tokens

        # Keep provider-native fields, and ensure normalized aliases exist.
        raw["prompt_tokens"] = prompt
        raw["completion_tokens"] = completion
        raw["total_tokens"] = total
        raw["prompt_audio_tokens"] = prompt_audio_tokens
        raw["prompt_cached_tokens"] = prompt_cached_tokens
        raw["prompt_cache_miss_tokens"] = prompt_cache_miss_tokens
        raw["prompt_text_tokens"] = prompt_text_tokens
        raw["prompt_image_tokens"] = prompt_image_tokens
        raw["completion_reasoning_tokens"] = completion_reasoning_tokens
        raw["completion_audio_tokens"] = completion_audio_tokens
        raw["completion_text_tokens"] = completion_text_tokens
        raw["completion_image_tokens"] = completion_image_tokens
        raw["accepted_prediction_tokens"] = accepted_prediction_tokens
        raw["rejected_prediction_tokens"] = rejected_prediction_tokens
        raw["cached_tokens"] = cached_tokens
        raw["cache_hit_tokens"] = cache_hit_tokens
        raw["cache_miss_tokens"] = cache_miss_tokens
        raw["reasoning_tokens"] = reasoning_tokens
        raw["accepted_tokens"] = accepted_tokens
        raw["rejected_tokens"] = rejected_tokens
        return raw

    def _usage_obj(self, usage: Any = None) -> LLMTokenUsage:
        d = self._last_usage if usage is None else self._usage_dict_from_any(usage)

        def _int_or_none(v: Any) -> Optional[int]:
            try:
                return None if v is None else int(v)
            except Exception:
                return None

        return LLMTokenUsage(
            prompt_tokens=_int_or_none(d.get("prompt_tokens")),
            completion_tokens=_int_or_none(d.get("completion_tokens")),
            total_tokens=_int_or_none(d.get("total_tokens")),
            prompt_audio_tokens=_int_or_none(d.get("prompt_audio_tokens")),
            prompt_cached_tokens=_int_or_none(d.get("prompt_cached_tokens")),
            prompt_cache_miss_tokens=_int_or_none(d.get("prompt_cache_miss_tokens")),
            prompt_text_tokens=_int_or_none(d.get("prompt_text_tokens")),
            prompt_image_tokens=_int_or_none(d.get("prompt_image_tokens")),
            completion_reasoning_tokens=_int_or_none(d.get("completion_reasoning_tokens")),
            completion_audio_tokens=_int_or_none(d.get("completion_audio_tokens")),
            completion_text_tokens=_int_or_none(d.get("completion_text_tokens")),
            completion_image_tokens=_int_or_none(d.get("completion_image_tokens")),
            accepted_prediction_tokens=_int_or_none(d.get("accepted_prediction_tokens")),
            rejected_prediction_tokens=_int_or_none(d.get("rejected_prediction_tokens")),
            cached_tokens=_int_or_none(d.get("cached_tokens")),
            cache_hit_tokens=_int_or_none(d.get("cache_hit_tokens")),
            cache_miss_tokens=_int_or_none(d.get("cache_miss_tokens")),
            reasoning_tokens=_int_or_none(d.get("reasoning_tokens")),
            accepted_tokens=_int_or_none(d.get("accepted_tokens")),
            rejected_tokens=_int_or_none(d.get("rejected_tokens")),
        )

    async def _with_retries(self, fn, *, method: str) -> Any:
        """
        Lightweight retry wrapper inspired by LangChain's ergonomics.
        """
        last_err: Optional[Exception] = None
        attempts = max(1, int(self._cfg.max_retries) + 1)
        for i in range(attempts):
            try:
                return await fn()
            except Exception as e:
                last_err = e
                if i >= attempts - 1:
                    break
                # exponential backoff + jitter
                base = float(self._cfg.retry_base_delay_s)
                delay = base * (2 ** i) * (0.75 + 0.5 * random.random())
                logger.warning("[llm:%s] call failed (attempt %s/%s), retrying in %.2fs: %s", method, i + 1, attempts,
                               delay, e)
                await asyncio.sleep(delay)
        raise cast(Exception, last_err)

    def _request_kwargs(self, request: LLMRequest) -> Dict[str, Any]:
        """
        Map `LLMRequest` into kwargs for OpenAI-compatible chat.completions.create.
        Falls back to self._cfg for default settings.
        """
        out: Dict[str, Any] = dict(request.extra or {})
        out["model"] = request.model if request.model else self._cfg.model
        
        req_temp = request.temperature if request.temperature is not None else self._cfg.temperature
        if req_temp is not None:
            out["temperature"] = float(req_temp)

        req_max_tokens = request.max_tokens if request.max_tokens is not None else self._cfg.max_tokens
        if req_max_tokens is not None:
            try:
                max_tokens = int(req_max_tokens)
            except Exception:
                max_tokens = None
            if max_tokens is not None and max_tokens > 0:
                out["max_tokens"] = max_tokens

        reason_value: Optional[bool] = None
        if "reason" in out:
            reason_value = bool(out.pop("reason"))
        elif request.reason is not None:
            reason_value = bool(request.reason)

        if reason_value is not None:
            extra_body = out.get("extra_body")
            if not isinstance(extra_body, dict):
                extra_body = {}
            if "reason" not in extra_body:
                extra_body["reason"] = reason_value
            out["extra_body"] = extra_body

        if request.tools is not None:
            out["tools"] = request.tools
        if request.tool_choice is not None:
            out["tool_choice"] = request.tool_choice
        return out

    def _build_resume_request(self, request: LLMRequest, partial_text: str) -> LLMRequest:
        resumed_messages = list(request.messages or [])
        trimmed = (partial_text or "").strip()
        if trimmed:
            resumed_messages.append({"role": "assistant", "content": trimmed})
        resumed_messages.append({"role": "user", "content": self._cfg.stream_resume_instruction})
        return LLMRequest(
            messages=resumed_messages,
            system_prompt=request.system_prompt,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            parse_json=request.parse_json,
            reason=request.reason,
            tools=request.tools,
            tool_choice=request.tool_choice,
            extra=dict(request.extra or {}),
        )

    async def complete(
        self,
        request: LLMRequest,
        *,
        on_chunk_delta_text: Optional[Callable[[str], Any]] = None,
        on_chunk_think: Optional[Callable[[str], Any]] = None,
        on_stream_end: Optional[Callable[[LLMResponse], Any]] = None,
    ) -> LLMResponse:
        """
        Canonical non-streaming API (preferred).
        """
        text_parts: List[str] = []
        think_parts: List[str] = []
        chunks: List[LLMStreamChunk] = []
        last_tool_calls: List[Dict[str, Any]] = []
        final_usage: Optional[LLMTokenUsage] = None
        last_raw_event: Any = None

        async for chunk in self.stream(request):
            chunks.append(chunk)
            if chunk.delta_text:
                text_parts.append(chunk.delta_text)
                if on_chunk_delta_text:
                    res_val = on_chunk_delta_text(chunk.delta_text)
                    if inspect.isawaitable(res_val):
                        await res_val
            if chunk.think:
                think_parts.append(chunk.think)
                if on_chunk_think:
                    res_val = on_chunk_think(chunk.think)
                    if inspect.isawaitable(res_val):
                        await res_val
            if chunk.tool_calls:
                last_tool_calls = list(chunk.tool_calls)
            if chunk.token_usage is not None:
                final_usage = chunk.token_usage
            if chunk.raw_event is not None:
                last_raw_event = chunk.raw_event

        text = "".join(text_parts)

        if request.parse_json:
            res = parse_json_from_model_output_detailed(text)
        else:
            res = LLMResponse(raw_text=text, content_text=text)

        res.token_usage = final_usage if final_usage is not None else self._usage_obj()
        res.tool_calls = last_tool_calls
        res.stream_chunks = chunks
        res.raw_completion = last_raw_event
        res.think = "".join(think_parts)

        if on_stream_end:
            res_val = on_stream_end(res)
            if inspect.isawaitable(res_val):
                await res_val

        return res

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """
        Streaming API.

        Best-effort real streaming for OpenAI-compatible servers.
        If provider does not support streaming, callers can switch to `complete()`.
        """
        client: AsyncOpenAI = self._ensure_client()
        kwargs = self._request_kwargs(request)

        stream_kwargs = dict(kwargs)
        stream_kwargs["stream"] = True
        stream_kwargs.setdefault("stream_options", {"include_usage": True})

        last_event: Optional[ChatCompletionChunk] = None
        final_usage: Optional[LLMTokenUsage] = None
        tool_call_store: Dict[str, Dict[str, Any]] = {}
        tool_call_order: List[str] = []
        tool_call_index_to_key: Dict[int, str] = {}
        aggregated_text: str = ""
        current_request = request
        restart_count = 0
        max_restarts = max(0, int(self._cfg.stream_max_restarts or 0))
        resume_enabled = bool(self._cfg.stream_resume_on_error)

        def _as_dict(obj: Any) -> Any:
            if obj is None:
                return None
            if isinstance(obj, dict):
                return obj
            if hasattr(obj, "model_dump"):
                try:
                    return obj.model_dump()
                except Exception:
                    return obj
            # best-effort: fallback to __dict__
            try:
                return dict(obj.__dict__)
            except Exception:
                return obj

        def _merge_tool_calls_delta(delta_tool_calls_obj: Any) -> List[Dict[str, Any]]:
            """
            Merge OpenAI-compatible streaming `delta.tool_calls` into an aggregated list.

            Delta items commonly look like:
            {"index":0,"id":"...","type":"function","function":{"name":"x","arguments":"{...partial..."}}
            """
            if not delta_tool_calls_obj:
                # return current snapshot
                return [tool_call_store[k] for k in tool_call_order]

            items = delta_tool_calls_obj
            # sometimes it's a single object
            if not isinstance(items, list):
                items = [items]

            for pos, it in enumerate(items):
                d = _as_dict(it)
                if not isinstance(d, dict):
                    continue
                explicit_id = d.get("id")
                delta_index = d.get("index")

                call_key: str
                if explicit_id and f"id_{explicit_id}" in tool_call_store:
                    call_key = f"id_{explicit_id}"
                elif isinstance(delta_index, int) and delta_index in tool_call_index_to_key:
                    call_key = tool_call_index_to_key[delta_index]
                elif explicit_id:
                    call_key = f"id_{explicit_id}"
                elif delta_index is not None:
                    call_key = f"index_{delta_index}"
                else:
                    # fall back to position inside this delta event
                    call_key = f"event_pos_{pos}"

                if call_key not in tool_call_store:
                    tool_call_store[call_key] = {
                        "id": explicit_id or call_key,
                        "type": d.get("type") or "function",
                        "function": {"name": "", "arguments": ""},
                    }
                    tool_call_order.append(call_key)

                entry = tool_call_store[call_key]
                if explicit_id and entry.get("id") != explicit_id:
                    entry["id"] = explicit_id
                if isinstance(delta_index, int):
                    tool_call_index_to_key[delta_index] = call_key
                fn = d.get("function") or {}
                if not isinstance(fn, dict):
                    fn = _as_dict(fn) or {}
                if isinstance(fn, dict):
                    name = fn.get("name")
                    if name:
                        entry["function"]["name"] = name
                    args = fn.get("arguments")
                    if args:
                        entry["function"]["arguments"] = (entry["function"].get("arguments") or "") + str(args)

                # allow other keys to be updated if present
                if d.get("type"):
                    entry["type"] = d.get("type")

            return [tool_call_store[k] for k in tool_call_order]

        def _extract_text_from_value(v: Any) -> str:
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                parts: List[str] = []
                for item in v:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        t = item.get("text") or item.get("content")
                        if isinstance(t, str):
                            parts.append(t)
                    else:
                        d = _as_dict(item)
                        if isinstance(d, dict):
                            t2 = d.get("text") or d.get("content")
                            if isinstance(t2, str):
                                parts.append(t2)
                return "".join(parts)
            return ""

        def _extract_delta_text_and_think(delta: Any) -> Tuple[str, str]:
            if delta is None:
                return "", ""

            delta_dict = _as_dict(delta)
            if isinstance(delta_dict, dict):
                text = _extract_text_from_value(delta_dict.get("content") or delta_dict.get("text"))
                think = ""
                for key in [
                    "reasoning_content",
                    "reasoning",
                    "thinking",
                    "thinking_content",
                    "reasoning_text",
                    "thought",
                ]:
                    think = _extract_text_from_value(delta_dict.get(key))
                    if think:
                        break
                return text, think

            text = _extract_text_from_value(getattr(delta, "content", None) or getattr(delta, "text", None))
            think = ""
            for key in [
                "reasoning_content",
                "reasoning",
                "thinking",
                "thinking_content",
                "reasoning_text",
                "thought",
            ]:
                think = _extract_text_from_value(getattr(delta, key, None))
                if think:
                    break
            return text, think

        def _extract_delta_tool_calls(delta: Any) -> Any:
            if delta is None:
                return None

            delta_dict = _as_dict(delta)
            if isinstance(delta_dict, dict):
                tc = delta_dict.get("tool_calls")
                if tc:
                    return tc
                fc = delta_dict.get("function_call")
                if fc:
                    return [{"index": 0, "type": "function", "function": fc}]
                return None

            tc = getattr(delta, "tool_calls", None)
            if tc:
                return tc
            fc = getattr(delta, "function_call", None)
            if fc:
                return [{"index": 0, "type": "function", "function": fc}]
            return None

        while True:
            async def _open_stream() -> Any:
                return await client.chat.completions.create(
                    messages=current_request.to_messages(),
                    **stream_kwargs
                )

            stream: Any = await self._with_retries(
                _open_stream,
                method="stream_open",
            )

            try:
                async for event in stream:
                    delta_text = ""
                    delta_think = ""
                    event: Any = event
                    last_event = event
                    delta_tool_calls: Any = None
                    aggregated_tool_calls: List[Dict[str, Any]] = []
                    try:
                        choice0 = event.choices[0]
                        delta = getattr(choice0, "delta", None)
                        delta_text, delta_think = _extract_delta_text_and_think(delta)
                        delta_tool_calls = _extract_delta_tool_calls(delta)
                        aggregated_tool_calls = _merge_tool_calls_delta(delta_tool_calls)
                    except Exception as e:
                        delta_text = ""
                        delta_think = ""
                        delta_tool_calls = None
                        aggregated_tool_calls = [tool_call_store[k] for k in tool_call_order]
                        logger.error("[llm:stream] failed to extract delta: %s", e)
                    # Some providers send usage only on the final event when stream_options.include_usage is enabled.
                    try:
                        usage_obj = getattr(event, "usage", None)
                        # Fallback: some SDKs/providers only expose usage via model_dump / extra fields.
                        if usage_obj is None and hasattr(event, "model_dump"):
                            try:
                                dumped = event.model_dump()
                                if isinstance(dumped, dict):
                                    usage_obj = dumped.get("usage") or dumped.get("x_openai_usage") or dumped.get("x_usage")
                            except Exception:
                                pass
                        if usage_obj is not None:
                            # also record into last_usage so callers relying on _usage_obj() can still work
                            self._record_usage(usage_obj, method="stream")
                            final_usage = self._usage_obj(usage_obj)
                    except Exception:
                        # Don't fail streaming if usage parsing fails.
                        pass

                    if delta_text or delta_think or delta_tool_calls:
                        if delta_text:
                            aggregated_text += str(delta_text)
                        yield LLMStreamChunk(
                            delta_text=delta_text,
                            think=delta_think,
                            delta_tool_calls=delta_tool_calls,
                            tool_calls=aggregated_tool_calls,
                            token_usage=None,
                            raw_event=event,
                            is_final=False,
                        )
                break
            except Exception as e:
                if not resume_enabled or restart_count >= max_restarts:
                    raise
                restart_count += 1
                logger.warning(
                    "[llm:stream] stream interrupted, attempting resume (%s/%s): %s",
                    restart_count,
                    max_restarts,
                    e,
                )
                current_request = self._build_resume_request(request, aggregated_text)
                continue

        # Final chunk carries best-effort usage + last raw event so callers can inspect finish_reason, tool_calls, etc.
        yield LLMStreamChunk(
            delta_text="",
            think="",
            delta_tool_calls=None,
            tool_calls=[tool_call_store[k] for k in tool_call_order],
            token_usage=final_usage,
            raw_event=last_event,
            is_final=True,
        )

    async def chat_completion(
            self,
            *,
            messages: Sequence[Dict[str, str]],
            **kwargs: Any,
    ) -> Any:
        """
        Return the raw ChatCompletion object.
        """
        client = self._ensure_client()
        model = str(kwargs.get("model") or self._cfg.model)
        temperature = float(kwargs.get("temperature", self._cfg.temperature))
        max_tokens = int(kwargs.get("max_tokens", self._cfg.max_tokens))

        async def _call() -> Any:
            return await client.chat.completions.create(
                model=model,
                messages=list(messages),
                temperature=temperature,
                max_tokens=max_tokens,
                **{k: v for k, v in kwargs.items() if k not in {"model", "temperature", "max_tokens"}},
            )

        resp: Any = await self._with_retries(_call, method="chat_completion")
        usage_obj = getattr(resp, "usage", None)
        if usage_obj is None and hasattr(resp, "model_dump"):
            try:
                dumped = resp.model_dump()
                if isinstance(dumped, dict):
                    usage_obj = dumped.get("usage") or dumped.get("x_openai_usage") or dumped.get("x_usage")
            except Exception:
                pass
        self._record_usage(usage_obj, method="chat_completion")
        return resp

    def _message_text(self, msg: Any) -> str:
        """
        Convert a ChatCompletionMessage content to plain text.
        Some providers may return list content; we best-effort stringify.
        """
        content = getattr(msg, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        # List/parts fallback
        try:
            return "".join(str(part) for part in content).strip()
        except Exception:
            return str(content).strip()

    # NOTE: `predict/chat/predict_stream` wrappers are provided as default methods on the Protocol.