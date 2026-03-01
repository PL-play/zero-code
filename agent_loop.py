"""CLI agent loop aligned with s02 tool-use pattern."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from dotenv import load_dotenv

from tools import TOOLS, dispatch_tool_call

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


class ZeroCodeAgent:
    """Tool-enabled agent that runs a while-loop until no tool_use."""

    def __init__(self) -> None:
        model = os.getenv("MODEL_ID")
        if not model:
            raise RuntimeError("缺少环境变量 MODEL_ID。")
        self.model = model
        self.client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
        self.system = (
            "You are a coding agent with access to workspace: "
            f"{Path.cwd().resolve()}. Use tools to solve tasks. Act, don't explain."
        )

    def agent_loop(self, messages: list[dict[str, Any]]) -> str:
        while True:
            response = self.client.messages.create(
                model=self.model,
                system=self.system,
                messages=messages,
                tools=TOOLS,
                max_tokens=8000,
            )
            messages.append({"role": "assistant", "content": response.content})
            has_tool_use = any(
                getattr(block, "type", None) == "tool_use" for block in response.content
            )
            if not has_tool_use:
                final_text = "\n".join(
                    block.text for block in response.content if hasattr(block, "text")
                ).strip()
                return final_text or "(no text output)"

            results = []
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                output = dispatch_tool_call(block.name, **block.input)
                print(f"> {block.name}: {output[:200]}")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )
            messages.append({"role": "user", "content": results})


def run_cli() -> None:
    try:
        agent = ZeroCodeAgent()
    except RuntimeError as exc:
        print(f"配置错误：{exc}")
        print("请先设置环境变量 MODEL_ID，再运行 CLI。")
        return

    print("Zero Code CLI 已启动。输入 exit 或 quit 退出。")
    history: list[dict[str, Any]] = []

    while True:
        try:
            query = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break

        if query.strip().lower() in {"exit", "quit", ""}:
            print("再见。")
            break

        history.append({"role": "user", "content": query})
        try:
            final_reply = agent.agent_loop(history)
        except Exception as exc:
            print(f"agent> Error: {exc}")
            continue
        print(f"agent> {final_reply}")
