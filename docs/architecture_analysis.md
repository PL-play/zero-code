# Zero-Code Agent 项目架构分析

## 项目概述

这是一个基于 Python 的交互式 CLI 编码 Agent 项目，核心功能是作为软件工程的智能助手，支持代码修复、功能开发、重构、代码解释等任务。项目采用了高度模块化和解耦的架构设计。

---

## 一、数据总线（Event Bus）

### 1.1 核心组件

**文件位置**: `core/events.py`, `core/types.py`

### 1.2 架构设计

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Agent Core     │────▶│  AgentEventBus   │────▶│  UI Adapters    │
│  (agent.py)     │     │  (DEFAULT_EVENT  │     │  (TUIAdapter,   │
│                 │     │   _BUS)          │     │   HeadlessUI)   │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │  Event Listeners │
                        │  (Subscribers)   │
                        └──────────────────┘
```

### 1.3 事件类型（AgentEventType）

```python
class AgentEventType(str, Enum):
    # 会话生命周期
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    
    # Round 生命周期
    ROUND_STARTED = "round_started"
    ROUND_COMPLETED = "round_completed"
    
    # 流式输出事件
    STREAM_STARTED = "stream_started"
    STREAM_DELTA = "stream_delta"          # 文本增量
    STREAM_COMPLETED = "stream_completed"
    STREAM_THINK_DELTA = "stream_think_delta"  # 思考过程增量
    
    # 工具调用事件
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_ERROR = "tool_call_error"
    
    # 状态事件
    USAGE_UPDATED = "usage_updated"
    STATUS_CHANGED = "status_changed"
    
    # 错误和通知
    AGENT_ERROR = "agent_error"
    USER_NOTIFICATION = "user_notification"
```

### 1.4 事件数据结构

```python
@dataclass
class AgentEvent:
    type: AgentEventType           # 事件类型
    payload: Dict[str, Any]        # 事件负载数据
    session_id: str | None         # 会话 ID
    round_id: Optional[int]        # Round 编号
    timestamp: float | None        # 时间戳
```

### 1.5 事件总线实现

```python
class AgentEventBus:
    def __init__(self):
        self._subscribers: List[Callable[[AgentEvent], None]] = []
    
    def subscribe(self, callback: Callable[[AgentEvent], None]):
        """订阅事件"""
        self._subscribers.append(callback)
    
    def publish(self, event: AgentEvent):
        """发布事件到所有订阅者"""
        for subscriber in self._subscribers:
            subscriber(event)
```

### 1.6 使用示例

在 `agent.py` 中的典型使用：

```python
# 发布会话开始事件
DEFAULT_EVENT_BUS.publish(
    AgentEvent(type=AgentEventType.SESSION_STARTED, payload={})
)

# 发布工具调用完成事件
DEFAULT_EVENT_BUS.publish(
    AgentEvent(
        type=AgentEventType.TOOL_CALL_COMPLETED,
        payload={
            "name": name,
            "output": output,
            "is_sub": is_sub,
            "tool_input": tool_input or {},
        },
    )
)

# 发布流式输出事件
DEFAULT_EVENT_BUS.publish(
    AgentEvent(
        type=AgentEventType.STREAM_DELTA,
        payload={"stream_id": "main", "text": text, "is_think": False},
    )
)
```

### 1.7 设计优势

- **解耦**: Agent 核心逻辑不直接依赖 UI 实现
- **可扩展**: 新的 UI 或监听器可以订阅事件而不修改核心代码
- **类型安全**: 使用 Enum 和 dataclass 确保事件结构一致性
- **调试友好**: 所有事件都有明确的时间戳和上下文信息

---

## 二、Hooks 系统

### 2.1 核心组件

**文件位置**: `core/hooks.py`

### 2.2 架构设计

```python
HookHandler = Callable[[Dict[str, Any]], Any] | Callable[[Dict[str, Any]], Awaitable[Any]]

class AgentHooks:
    """轻量级钩子管理器，用于 Agent 循环中的语义钩子点"""
    
    def __init__(self) -> None:
        self._handlers: Dict[str, List[Tuple[int, HookHandler]]] = {}
    
    def register(self, hook_point: str, handler: HookHandler, *, order: int = 0):
        """注册钩子处理器，支持优先级排序"""
        
    def run(self, hook_point: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """同步执行钩子"""
        
    async def run_async(self, hook_point: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """异步执行钩子"""
```

### 2.3 钩子执行流程

```
┌─────────────────┐
│  Agent Loop     │
│  (某个钩子点)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ AgentHooks.run()│
│ (按 order 排序)  │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌───────┐
│Handler│ │Handler│  (可修改 context)
│ order=0│ │order=5│
└───┬───┘ └───┬───┘
    │         │
    └────┬────┘
         ▼
┌─────────────────┐
│ 修改后的 context│
└─────────────────┘
```

### 2.4 钩子处理器特性

1. **支持同步和异步**: 自动检测 handler 是否为协程
2. **上下文传递**: 每个 handler 可以接收并修改共享的 context 字典
3. **优先级控制**: 通过 `order` 参数控制执行顺序
4. **可清除**: 支持清除特定或所有钩子点

### 2.5 使用示例

```python
from core.hooks import DEFAULT_HOOKS

# 注册钩子
DEFAULT_HOOKS.register(
    "before_llm_call",
    lambda ctx: {**ctx, "messages": add_system_prompt(ctx["messages"])},
    order=0
)

# 异步钩子
async def log_hook(context):
    print(f"LLM call with {len(context['messages'])} messages")
    return context

DEFAULT_HOOKS.register("before_llm_call", log_hook, order=10)

# 执行钩子
context = {"messages": [...], "model": "gpt-4"}
context = await DEFAULT_HOOKS.run_async("before_llm_call", context)
```

### 2.6 设计优势

- **非侵入式**: 钩子逻辑不污染核心 Agent 循环代码
- **灵活扩展**: 可以在不修改核心代码的情况下添加新功能
- **测试友好**: 钩子可以独立测试和模拟
- **热插拔**: 运行时可以注册/清除钩子

---

## 三、流式输出解耦

### 3.1 核心组件

**文件位置**: 
- `llm_client/interface.py` (LLMService Protocol)
- `llm_client/llm_factory.py` (OpenAICompatibleChatLLMService)
- `core/agent.py` (Agent Loop)
- `core/ui_adapter.py` (UIAdapter)

### 3.2 架构分层

```
┌─────────────────────────────────────────────────────────┐
│                    Agent Layer                          │
│  (core/agent.py)                                        │
│  - 调用 client.complete()                               │
│  - 传入 on_chunk_delta_text, on_chunk_think 回调        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                  LLM Client Layer                       │
│  (llm_client/interface.py)                              │
│  - LLMService Protocol                                  │
│  - complete(request, on_chunk_*, on_stream_end)         │
│  - stream(request) -> AsyncIterator[LLMStreamChunk]     │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Provider Implementation                    │
│  (llm_client/llm_factory.py)                            │
│  - OpenAICompatibleChatLLMService                       │
│  - 处理流式 API 调用                                     │
│  - 提取 delta_text, think, tool_calls                   │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│                    UI Layer                             │
│  (core/ui_adapter.py, core/tui.py)                      │
│  - UIAdapter.handle_stream_delta()                      │
│  - TUIAdapter.stream_text() / stream_think()            │
└─────────────────────────────────────────────────────────┘
```

### 3.3 流式数据流

```python
# 1. Agent 层调用
response = await client.complete(
    LLMRequest(messages=messages, ...),
    on_chunk_delta_text=getattr(UI, "stream_text", None),  # 文本回调
    on_chunk_think=getattr(UI, "stream_think", None),      # 思考回调
)

# 2. LLMService 层聚合流式块
async def complete(self, request, *, on_chunk_delta_text, on_chunk_think):
    async for chunk in self.stream(request):
        if chunk.delta_text:
            if on_chunk_delta_text:
                on_chunk_delta_text(chunk.delta_text)  # 触发 UI 回调
        if chunk.think:
            if on_chunk_think:
                on_chunk_think(chunk.think)  # 触发思考回调
    
# 3. UI 层接收并处理
def stream_text(self, text: str):
    # 发布事件总线事件
    DEFAULT_EVENT_BUS.publish(
        AgentEvent(
            type=AgentEventType.STREAM_DELTA,
            payload={"stream_id": "main", "text": text, "is_think": False},
        )
    )
    # 更新 TUI
    self._safe_dispatch("append_stream_text", text)
```

### 3.4 流式块数据结构

```python
@dataclass
class LLMStreamChunk:
    delta_text: str = ""              # 文本增量
    think: str = ""                   # 思考/推理文本
    delta_tool_calls: Any = None      # 工具调用增量
    tool_calls: List[Dict] = []       # 聚合的工具调用
    token_usage: Optional[LLMTokenUsage] = None  # Token 使用
    raw_event: Any = None             # 原始事件
    is_final: bool = False            # 是否为最后一个块
```

### 3.5 解耦关键点

1. **回调注入**: Agent 层不直接知道 UI 的存在，通过回调函数解耦
2. **Protocol 接口**: LLMService 使用 Protocol 定义接口，支持不同 Provider 实现
3. **统一数据格式**: LLMStreamChunk 统一了不同 Provider 的流式输出格式
4. **事件总线**: UI 层通过事件总线进一步解耦，支持多订阅者

### 3.6 流式恢复机制

```python
# 支持流式中断后恢复
stream_resume_on_error: bool = True
stream_max_restarts: int = 3
stream_resume_instruction: str = "继续，从你上次中断的位置继续输出"

# 在 llm_factory.py 中实现自动恢复逻辑
while True:
    try:
        async for event in stream:
            # 处理事件...
        break
    except Exception as e:
        if restart_count < max_restarts:
            # 构建恢复请求，包含已聚合的文本
            current_request = self._build_resume_request(request, aggregated_text)
            restart_count += 1
            continue
```

### 3.7 设计优势

- **Provider 无关**: 切换 LLM Provider 不影响 Agent 和 UI 代码
- **UI 无关**: 支持 TUI、Headless、Web 等多种 UI 实现
- **可测试**: 可以模拟流式输出进行单元测试
- **容错性**: 支持流式中断自动恢复

---

## 四、UI 抽象适配

### 4.1 核心组件

**文件位置**: 
- `core/ui_adapter.py` (UIAdapter Protocol)
- `core/tui.py` (TUIAdapter - Textual TUI 实现)
- `core/headless_ui.py` (HeadlessUI - 无头模式实现)

### 4.2 UIAdapter Protocol

```python
class UIAdapter(Protocol):
    """UI 抽象接口，Agent 核心通过此接口与 UI 交互"""
    
    def show_message(self, message: AgentMessage, *, elapsed: Optional[float] = None):
        """显示聊天消息"""
        
    def update_status(self, text: str):
        """更新状态栏"""
        
    def log_agent(self, text: str):
        """记录 Agent 日志"""
        
    def update_usage(self, usage_summary: str):
        """更新 Token 使用统计"""
        
    def show_tool_call_brief(self, name: str, brief: str):
        """显示工具调用简报"""
        
    def show_tool_call_detail(self, name: str, output: str, tool_input: dict = None):
        """显示工具调用详情"""
        
    def handle_stream_delta(self, stream_id: str, text: str, *, is_think: bool):
        """处理流式输出增量"""
```

### 4.3 UI 实现对比

| 功能 | TUIAdapter | HeadlessUI |
|------|-----------|------------|
| 聊天消息 | Textual Markdown 组件 | print() 到 stdout |
| 状态更新 | Textual 状态栏 | print() 到 stderr |
| 工具日志 | RichLog 组件 | print() 到 stderr |
| Token 统计 | 专用面板 | print() 到 stderr |
| 流式输出 | 异步更新 Textarea | print() 带 flush |
| 文件变更追踪 | Git 状态集成 | 无 |
| 图片预览 | 内嵌预览 | 无 |

### 4.4 TUIAdapter 关键特性

```python
class TUIAdapter:
    """Textual TUI 适配器，同时实现 UIAdapter 协议"""
    
    def __init__(self):
        self._current_todo = ""
        self._tool_buffer = []
        self._file_stats: dict[str, dict] = {}  # 文件变更统计
        self.app = None  # Textual App 实例
    
    def set_app(self, app):
        """链接 Textual App 实例"""
        
    def _safe_dispatch(self, method_name: str, *args, **kwargs):
        """线程安全地调用 Textual App 方法"""
        # 检测是否在主线程/事件循环中
        try:
            loop = asyncio.get_running_loop()
            is_async = True
        except RuntimeError:
            is_async = False
            
        if is_async and threading.current_thread() is threading.main_thread():
            method(*args, **kwargs)  # 直接调用
        else:
            self.app.call_from_thread(method, *args, **kwargs)  # 线程安全调用
```

### 4.5 线程安全机制

```
┌─────────────────┐
│   Agent Thread  │  (运行 agent_loop)
└────────┬────────┘
         │
         │ 调用 UI.tool_call()
         │
         ▼
┌─────────────────┐
│  TUIAdapter     │
│  _safe_dispatch │
└────────┬────────┘
         │
    ┌────┴────┐
    │ 检测线程 │
    └────┬────┘
         │
    ┌────┴────┐
    ▼         ▼
主线程     非主线程
    │         │
    │         ▼
    │    call_from_thread()
    │         │
    ▼         ▼
┌─────────────────┐
│  Textual App    │  (主事件循环)
│  (异步更新 UI)  │
└─────────────────┘
```

### 4.6 UI 切换示例

```python
# 使用 TUI
from core.tui import TUIAdapter
UI = TUIAdapter()

# 使用 Headless
from core.headless_ui import HeadlessUI
UI = HeadlessUI()

# Agent 代码无需修改
UI.show_message(AgentMessage(role="assistant", content="Hello"))
UI.update_status("Running...")
```

### 4.7 设计优势

- **Protocol 驱动**: 使用 Protocol 而非抽象类，更灵活
- **类型安全**: 静态类型检查确保 UI 实现完整性
- **可替换**: 可以轻松切换不同的 UI 实现
- **向后兼容**: TUIAdapter 保留了旧版 ConsoleUI 的 API
- **线程安全**: 正确处理 Agent 线程和 UI 线程的交互

---

## 五、整体架构总结

### 5.1 完整架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Interface                          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │   Textual TUI   │  │   Headless UI   │  │  Future Web UI  │ │
│  │  (core/tui.py)  │  │ (headless_ui.py)│  │  (to be added)  │ │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘ │
└───────────┼────────────────────┼────────────────────┼───────────┘
            │                    │                    │
            └────────────────────┼────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │     UIAdapter Protocol  │
                    │    (core/ui_adapter.py) │
                    └────────────┬────────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            │                    │                    │
            ▼                    ▼                    ▼
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│   Event Bus       │ │   Hooks System    │ │   Agent Core      │
│ (core/events.py)  │ │  (core/hooks.py)  │ │  (core/agent.py)  │
│                   │ │                   │ │                   │
│ - SESSION_STARTED │ │ - before_llm_call │ │ - Agent Loop      │
│ - STREAM_DELTA    │ │ - after_tool_call │ │ - Tool Execution  │
│ - TOOL_CALL_DONE  │ │ - ...             │ │ - Sub-agent       │
└───────────────────┘ └───────────────────┘ └─────────┬─────────┘
                                                      │
                                                      ▼
                    ┌─────────────────────────────────────────────┐
                    │           LLM Client Layer                  │
                    │         (llm_client/interface.py)           │
                    │                                             │
                    │  - LLMService Protocol                      │
                    │  - LLMRequest / LLMResponse                 │
                    │  - LLMStreamChunk                           │
                    └────────────────────┬────────────────────────┘
                                         │
                    ┌────────────────────┼────────────────────┐
                    │                    │                    │
                    ▼                    ▼                    ▼
          ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
          │ OpenAI Compatible│ │   Qwen Image    │ │   Web Search    │
          │  (llm_factory)  │ │  (qwen_image.py)│ │ (web_search.py) │
          └─────────────────┘ └─────────────────┘ └─────────────────┘
```

### 5.2 关键设计模式

| 模式 | 应用场景 | 实现位置 |
|------|---------|---------|
| **Protocol** | UI 接口、LLM 服务接口 | `ui_adapter.py`, `interface.py` |
| **Event Bus** | 组件间通信 | `events.py` |
| **Hook Pattern** | 生命周期扩展点 | `hooks.py` |
| **Adapter** | UI 适配 | `tui.py`, `headless_ui.py` |
| **Strategy** | LLM Provider 切换 | `llm_factory.py` |
| **Observer** | 事件订阅 | `events.py` |

### 5.3 数据流示例：用户发送消息到 Agent 回复

```
1. 用户输入
   │
   ▼
2. TUI 接收输入 → 构建 AgentMessage
   │
   ▼
3. 添加到 messages 列表 → 调用 agent_loop()
   │
   ▼
4. Agent Loop:
   - 发布 ROUND_STARTED 事件
   - 调用 CTX.microcompact() 压缩上下文
   - 调用 client.complete()
   │
   ▼
5. LLM Client:
   - 构建 LLMRequest
   - 调用 OpenAI API (流式)
   - 对每个 chunk:
     * 提取 delta_text / think
     * 调用 on_chunk_delta_text() 回调
   │
   ▼
6. UI Adapter (回调中):
   - 发布 STREAM_DELTA 事件
   - 调用 _safe_dispatch("append_stream_text", text)
   │
   ▼
7. Textual TUI:
   - 更新 Textarea 组件
   - 实时显示 Agent 回复
   │
   ▼
8. LLM 完成:
   - 返回 LLMResponse
   - 更新 Token 统计
   - 发布 USAGE_UPDATED 事件
   │
   ▼
9. Agent Loop:
   - 检查是否有 tool_calls
   - 如果有：执行工具 → 添加 tool 消息 → 继续下一轮
   - 如果没有：返回最终回复
   │
   ▼
10. TUI 显示完整回复 → 等待用户下一条消息
```

### 5.4 扩展性分析

#### 添加新的 UI

1. 实现 `UIAdapter` Protocol
2. 实现所有必需方法
3. 在运行时替换全局 `UI` 实例

```python
class WebUI(UIAdapter):
    def show_message(self, message, *, elapsed=None):
        # WebSocket 推送消息到前端
        websocket.send_json({"type": "message", "data": message.dict()})
    
    # ... 实现其他方法

UI = WebUI()
```

#### 添加新的 LLM Provider

1. 实现 `LLMService` Protocol
2. 实现 `complete()` 和 `stream()` 方法
3. 在 `llm_factory.py` 中注册

```python
class AnthropicLLMService(LLMService):
    async def complete(self, request, **kwargs):
        # 调用 Anthropic API
        ...
    
    async def stream(self, request):
        # 流式调用 Anthropic API
        ...
```

#### 添加新的 Hook

```python
# 在 agent.py 的适当位置
context = {"messages": messages, "round": round_idx}
context = DEFAULT_HOOKS.run_async("before_round", context)
messages = context["messages"]
```

---

## 六、总结

### 6.1 架构优势

1. **高度解耦**: Agent 核心、UI、LLM Provider 完全分离
2. **类型安全**: 全面使用 Type Hints 和 Protocol
3. **可扩展**: 通过 Event Bus 和 Hooks 轻松扩展功能
4. **可测试**: 各层可独立测试，支持 Mock
5. **多 UI 支持**: TUI、Headless、未来 Web UI 可无缝切换
6. **流式友好**: 完整的流式输出支持，包括思考和工具调用
7. **线程安全**: 正确处理异步 Agent 和同步 UI 的交互

### 6.2 适用场景

- ✅ CLI 编码助手
- ✅ 自动化脚本执行
- ✅ 代码审查和分析
- ✅ 文档生成和维护
- ✅ 多模态任务（图片生成/编辑）
- ✅ 网络搜索增强

### 6.3 改进建议

1. **持久化**: 添加会话持久化，支持中断恢复
2. **多会话**: 支持并发多个 Agent 会话
3. **插件系统**: 基于 Hooks 构建插件生态
4. **性能优化**: 流式输出的批量更新优化
5. **监控**: 添加更详细的性能指标和日志

---

*文档生成时间：2024*
*项目版本：基于当前代码分析*
