"""Bundled in-process UI (reference implementation: Textual).

:class:`core.application.Config` 与 :class:`core.application.AgentLoop` **不依赖**本模块；
这是「在**不改 core 源码**的前提下」把默认 UI 挂到 ``Config`` 上的方式。

用法（产品入口或你自己的 main）::

    from core.application import AgentLoop, Config
    from core.ui.bundled_process_frontend import install_bundled_process_frontend

    cfg = Config()
    install_bundled_process_frontend(cfg)
    AgentLoop(cfg).run()

---------------------------------------------------------------------------
事件契约（agent core → bus → 本前端）
---------------------------------------------------------------------------

当前参考 UI 在 :meth:`core.tui.ZeroCodeApp.on_mount` 里订阅以下
:class:`~core.types.AgentEventType`（若你自写 UI，至少需处理你关心的子集）：

- ``SESSION_STARTED`` — 新轮次/会话开始，清空工具缓冲等
- ``STREAM_STARTED`` / ``STREAM_DELTA`` / ``STREAM_THINK_DELTA`` / ``STREAM_COMPLETED`` — 流式输出
- ``TOOL_CALL_COMPLETED`` — 工具名、输出、是否子 agent、tool_input
- ``STATUS_CHANGED`` — 状态栏文案
- ``USAGE_UPDATED`` — token 用量摘要
- ``TODO_UPDATED`` — todo 面板文本
- ``USER_NOTIFICATION`` — 提醒（如 todo 未更新）
- ``ROUND_TOOLS_PRESENT`` — 本轮是否有 tool_calls
- ``SUBAGENT_TASK_START`` / ``SUBAGENT_TEXT`` / ``SUBAGENT_LIMIT`` — 子 agent
- ``SYSTEM_LOG`` — 调试日志
- ``SESSION_ENDED`` — 会话结束

Agent 核心另会通过 :func:`core.agent_context.get_event_bus` 发布上述类型事件；
你的前端应对 ``config.event_bus`` 调用 ``subscribe``，与参考实现共用同一实例。

---------------------------------------------------------------------------
钩子（AgentHooks）
---------------------------------------------------------------------------

当前 **agent 主循环未调用** :meth:`core.hooks.AgentHooks.run` 等（无强制 hook 契约）。
若日后 core 在固定 hook_point 上调用 ``get_hooks()``，前端可在 ``cfg.register_hook(...)``
里扩展；本 bundled UI **今天不依赖**任何 hook_point。

---------------------------------------------------------------------------
生命周期
---------------------------------------------------------------------------

``install_bundled_process_frontend`` 只做一件事：向 ``config.on_start`` 追加一个会
阻塞运行的启动函数（内部创建 Textual ``ZeroCodeApp``、:func:`core.state.UI.set_app`、
``app.run()``）。多进程/多前端请自行拆分进程并换用队列或 IPC，而非本模块。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.application import Config


def install_bundled_process_frontend(config: Config) -> Config:
    """把默认进程内 UI 挂到 ``config`` 上（链式，返回同一 ``config``）。"""
    from core.ui.textual_startup import start_textual_app

    return config.on_app_start(start_textual_app)
