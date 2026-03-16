# OpenCode Code Agent 消息记忆管理设计与实现分析

> 基于 [opencode](https://github.com/anomalyco/opencode) 项目源码的深度分析  
> 分析日期：2026-03-14  
> 分析重点：会话存储、消息建模、上下文压缩、恢复与持续执行机制

---

## 目录

1. [项目架构概览](#1-项目架构概览)
2. [消息模型：为什么 OpenCode 采用 MessageV2](#2-消息模型为什么-opencode-采用-messagev2)
3. [记忆存储机制](#3-记忆存储机制)
   - 3.1 [SQLite 三表存储：session / message / part](#31-sqlite-三表存储session--message--part)
   - 3.2 [消息流式读取与会话恢复](#32-消息流式读取与会话恢复)
   - 3.3 [分叉策略：不是树内分支，而是 Session Fork](#33-分叉策略不是树内分支而是-session-fork)
4. [上下文构造机制](#4-上下文构造机制)
   - 4.1 [用户输入如何被展开为上下文部件](#41-用户输入如何被展开为上下文部件)
   - 4.2 [系统提示、指令文件与环境注入](#42-系统提示指令文件与环境注入)
   - 4.3 [从 MessageV2 到 ModelMessage 的转换](#43-从-messagev2-到-modelmessage-的转换)
5. [上下文压缩机制（Compaction）](#5-上下文压缩机制compaction)
   - 5.1 [触发条件](#51-触发条件)
   - 5.2 [压缩任务的插入方式](#52-压缩任务的插入方式)
   - 5.3 [压缩执行流程](#53-压缩执行流程)
   - 5.4 [压缩后的上下文恢复](#54-压缩后的上下文恢复)
6. [压缩之外的“轻量记忆控制”](#6-压缩之外的轻量记忆控制)
   - 6.1 [旧工具输出裁剪（Prune）](#61-旧工具输出裁剪prune)
   - 6.2 [大输出截断与外部落盘](#62-大输出截断与外部落盘)
   - 6.3 [Provider 级消息归一化与缓存提示](#63-provider-级消息归一化与缓存提示)
7. [Agent 循环中的消息记忆流转](#7-agent-循环中的消息记忆流转)
8. [错误处理、重试与溢出恢复](#8-错误处理重试与溢出恢复)
9. [关键设计决策与启示](#9-关键设计决策与启示)
10. [与 Pi-Mono / Claude Code 的对比摘要](#10-与-pi-mono--claude-code-的对比摘要)
    - 10.1 [逐项对照表](#101-逐项对照表)
    - 10.2 [简要对比](#102-简要对比)
11. [附录：关键源码位置](#11-附录关键源码位置)

---

## 1. 项目架构概览

OpenCode 的 Code Agent 采用比较清晰的分层结构，消息记忆管理主要落在 `packages/opencode/src/session/` 内：

```
┌────────────────────────────────────────────────────────────┐
│ SessionPrompt                                              │
│ 主循环、构造用户消息、选择上下文、触发压缩/继续执行        │
├────────────────────────────────────────────────────────────┤
│ SessionProcessor                                           │
│ 流式消费 LLM 输出，将 text/reasoning/tool 写回消息 parts    │
├────────────────────────────────────────────────────────────┤
│ MessageV2 / Session                                        │
│ 消息模型、SQLite 持久化、消息流恢复、usage 统计             │
├────────────────────────────────────────────────────────────┤
│ SessionCompaction / SessionSummary / Truncate              │
│ 压缩、diff 摘要、旧工具输出裁剪、大输出落盘                 │
├────────────────────────────────────────────────────────────┤
│ LLM / ProviderTransform                                    │
│ 发送给模型前的消息归一化、provider 差异适配、缓存提示       │
└────────────────────────────────────────────────────────────┘
```

它的核心思想不是维护一个“抽象记忆对象”，而是：

- 用 `session + message + part` 把对话拆成结构化事件流；
- 用 `MessageV2.filterCompacted()` 在恢复上下文时截断旧历史；
- 用压缩摘要 assistant 消息替代大段旧消息；
- 再用工具输出裁剪、Truncate、provider cache hint 等手段减少上下文压力。

---

## 2. 消息模型：为什么 OpenCode 采用 MessageV2

OpenCode 当前的关键实现是 `message-v2.ts`，相比旧版 `message.ts`，它把“消息”拆成了：

- **消息头（Info）**
  - `user`
  - `assistant`
- **消息体部件（Part）**
  - `text`
  - `reasoning`
  - `tool`
  - `file`
  - `subtask`
  - `compaction`
  - `retry`
  - `step-start`
  - `step-finish`
  - `patch`
  - `snapshot`
  - `agent`

这种设计有几个非常关键的好处：

1. **流式写入天然友好**  
   LLM 输出文本、思维链、工具调用、工具结果本来就是分段到达的；拆成 parts 后可以边流式消费边落盘，而不必等待整条 assistant 消息完成。

2. **记忆可“部分失忆”**  
   例如工具结果被裁剪后，只需要在 `tool.state.time.compacted` 上打标，而不必删整条消息。

3. **同一条消息可以同时承载多种语义**  
   一条 assistant 消息内部可以顺序记录：
   - `step-start`
   - `reasoning`
   - `text`
   - `tool`
   - `step-finish`
   - `patch`

4. **更适合恢复 UI 和继续执行**  
   因为前端/控制层可以根据 part 类型恢复“这轮到底发生了什么”，不仅是恢复文本对话。

从“记忆管理”的角度看，OpenCode 保存的不是纯聊天 transcript，而是**可执行对话的事件日志**。

---

## 3. 记忆存储机制

### 3.1 SQLite 三表存储：session / message / part

OpenCode 不像 Pi-Mono 使用 JSONL append-only 文件，而是使用 SQLite：

- `SessionTable`
- `MessageTable`
- `PartTable`

其中：

- `session` 保存会话级元数据：标题、project、summary、权限、是否 compacting、是否 archived 等。
- `message` 保存消息级头信息：role、model、agent、tokens、error、summary 标记等。
- `part` 保存消息的具体内容块：文本、推理、工具调用结果、压缩标记等。

额外还有两个与“会话记忆”密切相关的存储侧面：

- `Storage.write(["session_diff", sessionID], diffs)`：把会话 diff 结果单独落到存储中；
- `TodoTable`：把 todo 作为会话伴随状态保存。

这意味着 OpenCode 的“记忆”分为两层：

- **主对话记忆**：在 SQLite 三表中；
- **派生记忆/分析结果**：如 diff、外部工具大输出，在独立存储中。

### 3.2 消息流式读取与会话恢复

`MessageV2.stream(sessionID)` 以分页方式读取消息：

- 每次取 50 条 message；
- 再批量取对应 parts；
- 按时间倒序遍历；
- 上层再 reverse 回正序。

`Session.messages()` 进一步把整个会话恢复成 `MessageV2.WithParts[]`。

更关键的是：**真正送给模型之前，并不会直接使用全量历史**。  
OpenCode 会先执行：

```typescript
let msgs = await MessageV2.filterCompacted(MessageV2.stream(sessionID))
```

`filterCompacted()` 的逻辑很重要：

- 它从最新消息向旧消息回扫；
- 一旦发现“已经完成的压缩摘要 assistant 消息”；
- 再继续往前扫到对应的 `compaction` user 消息；
- 到这里就停止，不再读取更老历史；
- 最后 reverse 成新的上下文数组。

也就是说，**压缩后的历史恢复不是“旧历史 + 摘要一起保留”，而是把摘要之前的旧消息整个切掉**。  
最新上下文通常只保留：

- 一条 `compaction` user 消息；
- 对应的 `summary: true` assistant 摘要消息；
- 压缩后的新消息。

### 3.3 分叉策略：不是树内分支，而是 Session Fork

这点和 Pi-Mono 差异非常大。

Pi-Mono 是单个 session 文件中的树形分支；  
OpenCode 则是**线性 session + 外部 fork**：

- `Session.fork({ sessionID, messageID? })`
- 复制原会话中到某个消息为止的全部 message / part；
- 重写消息 id 与 parentID；
- 生成一个新的 session。

所以 OpenCode 的分支模型是：

- **单会话内部保持线性**
- **需要分叉时复制出一个新 session**

优点是实现简单、读取上下文简单；  
代价是没有 Pi-Mono 那种“同一会话内部廉价分叉 + 路径切换”的能力。

---

## 4. 上下文构造机制

### 4.1 用户输入如何被展开为上下文部件

`SessionPrompt.createUserMessage()` 是 OpenCode 记忆构造的关键入口。

用户输入的 `parts` 在写入数据库前会被“展开”为更丰富的结构：

- 普通文本 -> `text`
- 本地文件 -> 自动调用 Read 工具，写入
  - `Called the Read tool with the following input: ...`
  - 读取结果文本
  - 文件 part / 附件 part
- 目录 -> 自动列目录并写入 synthetic 文本
- MCP resource -> 读取资源后写入 synthetic 文本
- `@agent` -> 写入 `agent` part + synthetic 提示，引导模型调用 task subagent

也就是说，OpenCode 的“记忆”并不是只记住“用户引用了一个文件”，而是尽量把**文件内容本身**折叠进消息 parts。

这是一个非常实用的策略：

- 短期内能减少模型反复 read 文件的需要；
- 但也会快速膨胀上下文，因此后面必须依赖 compaction 与 prune。

### 4.2 系统提示、指令文件与环境注入

OpenCode 在每轮请求前都会拼接系统上下文，主要来自两部分：

1. `SystemPrompt.environment(model)`
   - 当前模型名
   - 工作目录
   - 是否是 git repo
   - 平台
   - 当天日期

2. `InstructionPrompt.system()`
   - `AGENTS.md`
   - `CLAUDE.md`
   - `CONTEXT.md`（兼容旧名）
   - config 中声明的 instruction 文件或 URL

另外还有一个更隐蔽但很重要的机制：`InstructionPrompt.resolve(messages, filepath, messageID)` 会在读某个文件时，向上查找同目录链路上的 `AGENTS.md / CLAUDE.md`，作为局部指令上下文补充。

这意味着 OpenCode 的记忆体系不止是“聊天历史”，还包括：

- 全局 instruction
- 项目 instruction
- 文件附近 instruction

它把“文件系统中的说明文档”视为长期记忆的一部分。

### 4.3 从 MessageV2 到 ModelMessage 的转换

`MessageV2.toModelMessages()` 决定哪些持久化消息真正进入 LLM 上下文。

关键转换包括：

- user `text` -> 普通文本输入
- user `file` -> 模型可读文件/媒体输入
- user `compaction` -> 文本 `"What did we do so far?"`
- user `subtask` -> 文本 `"The following tool was executed by the user"`
- assistant `tool` -> tool-call / tool-result 语义块
- 被 prune 的工具结果 -> `" [Old tool result content cleared] "`
- 中断中的 tool 调用 -> 伪造一个 error tool-result，避免 provider 悬空

还有两个非常细的兼容设计：

1. **媒体附件兼容**  
   如果 provider 不支持在 tool result 中承载图片/PDF，就把这些附件抽成额外的 user message 注入。

2. **跨模型恢复**  
   如果历史消息来自不同 provider/model，会丢弃部分 provider metadata，避免污染新的模型调用。

---

## 5. 上下文压缩机制（Compaction）

### 5.1 触发条件

压缩判断在 `SessionCompaction.isOverflow()` 中完成。

核心逻辑：

```typescript
count = total || input + output + cache.read + cache.write
reserved = config.compaction?.reserved ?? min(20000, maxOutputTokens(model))
usable = model.limit.input ? model.limit.input - reserved : context - maxOutputTokens(model)
return count >= usable
```

特点：

- 默认会预留一段 **reserved token buffer**；
- buffer 默认是 `min(20_000, maxOutputTokens(model))`；
- 可以通过 `config.compaction.reserved` 覆盖；
- 若 `config.compaction.auto === false`，则彻底关闭自动压缩。

它和 Claude Code 的 92% 比例阈值不同，OpenCode更接近：

- **根据模型 input/context 上限**
- **减去输出预留**
- **再看当前 usage 是否越界**

### 5.2 压缩任务的插入方式

OpenCode 没有在发现超限时立刻原地替换上下文，而是先创建一个**压缩任务消息**：

- 新建一条 user message；
- 其中插入一个 `type: "compaction"` 的 part；
- 标记 `auto: true/false`。

随后 `SessionPrompt.loop()` 在下一轮主循环里发现这个 pending compaction task，再进入 `SessionCompaction.process()`。

这种设计的好处是：

- 压缩本身也成为会话历史的一部分；
- UI 和恢复逻辑能看见“这里发生过一次压缩”；
- compaction 可以像普通任务一样被调度。

### 5.3 压缩执行流程

`SessionCompaction.process()` 的流程如下：

1. 取触发压缩的 user message。
2. 选择 compaction agent/model。
   - 优先 `Agent.get("compaction")` 的专用模型；
   - 否则回退到原 user message 的模型。
3. 创建一条新的 assistant 消息，标记：
   - `summary: true`
   - `agent: "compaction"`
   - `mode: "compaction"`
4. 调用 `SessionProcessor.process()`，把：
   - 当前压缩边界内的全部 `messages`
   - 再加一个新的 user prompt
   一起送给模型。

默认压缩提示要求模型输出：

- 用户目标
- 关键指令
- 发现
- 已完成/进行中/待完成事项
- 相关文件/目录

此外还支持插件钩子：

- `experimental.session.compacting`

插件可以：

- 注入额外 compaction context；
- 替换默认压缩 prompt。

如果是自动压缩且压缩成功，OpenCode 还会再插入一条 synthetic user message：

`Continue if you have next steps...`

这相当于在压缩后自动推动 agent 继续执行，而不是停在摘要处。

### 5.4 压缩后的上下文恢复

OpenCode 的恢复策略很值得注意。

压缩后没有显式“重建最近 N 条原始消息”的逻辑，而是依赖三件事：

1. `compaction` user message 仍保留在历史中；
2. 新生成的 `summary: true` assistant 消息保留在历史中；
3. `MessageV2.filterCompacted()` 在后续读取时，只保留这对 compaction 边界之后的历史。

再经过 `toModelMessages()` 转换后：

- `compaction` part 会被转成 `"What did we do so far?"`
- 紧随其后的 assistant summary 会提供摘要正文

于是新的 LLM 上下文就变成：

```text
user: What did we do so far?
assistant: <压缩摘要>
... 后续消息 ...
```

这是一种非常“对话化”的恢复方式：

- 不需要特殊的 summary message 类型映射到 system；
- 直接利用 user/assistant 语义对让模型继续会话。

---

## 6. 压缩之外的“轻量记忆控制”

### 6.1 旧工具输出裁剪（Prune）

OpenCode 不是只靠 compaction 控制上下文。

`SessionCompaction.prune()` 还会做一种更轻量的“旧工具结果失忆”：

- 从最新消息向前扫描；
- 至少跨过 2 个 user turn 才开始考虑裁剪；
- 遇到已完成的 `summary` assistant 就停止；
- 只统计已完成 tool part 的 output token；
- 先保留最近约 `40_000` tokens 的工具结果；
- 超出的老工具结果标记 `part.state.time.compacted = now`；
- 只有累计可裁剪量超过 `20_000` tokens 才真正执行。

特殊保护：

- `skill` 工具不会被 prune。

一旦 tool part 被标记为 compacted，`toModelMessages()` 会把其输出替换为：

`[Old tool result content cleared]`

这比整轮压缩更细粒度，适合清理“历史 shell 输出、web 内容、大段 read 结果”。

### 6.2 大输出截断与外部落盘

`Truncate.output()` 是压缩前的第一道防线。

默认限制：

- 最多 `2000` 行
- 最多 `50KB`

超出时：

- 只保留 head 或 tail 预览；
- 全量输出写入 `Global.Path.data/tool-output/`；
- 在返回给模型的文本里插入提示，告诉 agent：
  - 完整输出保存在哪里；
  - 应该用 grep/read 或 task subagent 去看局部，而不是整文件读入。

这其实是一个很聪明的“上下文预算外置”策略：

- 大输出仍可追溯；
- 但默认不塞进上下文。

### 6.3 Provider 级消息归一化与缓存提示

`ProviderTransform.message()` 还做了两类和记忆相关的优化：

1. **provider 兼容归一化**
   - 过滤空消息
   - 规范 toolCallId
   - 对不支持某类输入的模型改写为错误文本
   - 处理 reasoning 字段转存

2. **ephemeral cache hint**
   - 对 Anthropic / OpenRouter / Bedrock / OpenAI-compatible 等 provider，
   - 对前两个 system message 和最后两个非 system message 标记 cache control。

这不改变“逻辑记忆”，但会明显影响：

- provider 端缓存命中；
- 上下文传输成本；
- 长会话响应延迟。

---

## 7. Agent 循环中的消息记忆流转

`SessionPrompt.loop()` 基本定义了 OpenCode 的记忆生命周期：

1. 从数据库恢复消息；
2. `filterCompacted()` 切掉压缩边界之前的历史；
3. 找到最后一个 user、assistant、lastFinished；
4. 若存在 pending `subtask` 或 `compaction`，优先处理任务；
5. 若 lastFinished 已经接近溢出，则先插入 compaction task；
6. 否则进入正常 LLM 处理：
   - 插入 reminders
   - resolve tools
   - 构建 system prompt
   - `MessageV2.toModelMessages(msgs, model)`
   - 调 `SessionProcessor.process()`
7. 结束后执行 `SessionCompaction.prune()`。

所以 OpenCode 的记忆不是“每轮把历史原样发给模型”，而是每轮都经过一次：

- 恢复
- 截断
- 注入
- 归一化
- 再持久化

这是一条持续演化的消息流水线。

---

## 8. 错误处理、重试与溢出恢复

`SessionRetry` 负责自动重试：

- 初始延迟 2 秒
- 指数退避
- 支持读取 `retry-after` / `retry-after-ms`
- 对 retryable APIError 自动重试

一个关键点是：

- **ContextOverflowError 不会进入 retry**

这符合“溢出应通过压缩解决，而不是重试”的原则。

不过当前实现里还有一个值得注意的细节：

- `SessionProcessor.process()` 在 `catch` 中识别到 `ContextOverflowError` 后，只有一个 `TODO`
- 也就是说，对“provider 实际返回的上下文溢出错误”的立即恢复路径，并没有像 Claude Code 那样做得很完整
- 当前更依赖前面的**预防性溢出检查**：在每轮开始前用上一次 usage 提前触发 compaction

这说明 OpenCode 当前的压缩策略更偏：

- **预防性**
- 而不是 **事后补救型**

从工程上讲，这已经足够实用，但鲁棒性上仍有继续增强空间。

---

## 9. 关键设计决策与启示

### 9.1 用 part 化消息替代单条 transcript

OpenCode 的真正资产不是“聊天记录”，而是：

- 文本
- 推理
- 工具输入输出
- patch / snapshot
- subtask / compaction

组成的结构化事件流。  
这让它能更自然地支持 UI 恢复、工具状态恢复和上下文选择。

### 9.2 压缩边界是一对 user/assistant 消息

相比单独引入“summary entry”这种内部类型，OpenCode 的做法更简单：

- user：`What did we do so far?`
- assistant：压缩摘要

这让恢复逻辑几乎完全复用普通消息管道。

### 9.3 不做树形会话，而做外部 fork

OpenCode 牺牲了 Pi-Mono 那种树内分支能力，换来：

- simpler storage
- simpler retrieval
- simpler compaction boundary

对于偏“持续编码执行”的 CLI agent，这个取舍很合理。

### 9.4 先截断大输出，再考虑压缩历史

`Truncate` 和 `prune` 的存在说明 OpenCode 把上下文管理分成三层：

1. 单次工具输出不要太大；
2. 老工具输出可以部分失忆；
3. 整体历史过大时再做 compaction。

这是很实用的分层治理思路。

### 9.5 文件系统说明文档被纳入长期记忆

`AGENTS.md`、`CLAUDE.md`、局部 instruction 文件，本质上构成了 OpenCode 的长期记忆层。  
它不是一个独立 memory store，但效果上承担了“跨轮次、跨会话持续约束 agent 行为”的作用。

---

## 10. 与 Pi-Mono / Claude Code 的对比摘要

### 10.1 逐项对照表

| 维度 | Pi-Mono | Claude Code | OpenCode |
|------|---------|--------------|----------|
| **存储形态** | JSONL 追加日志，单文件 | 运行时 `messages[]` + 持久化（逆向推测） | SQLite 三表（session / message / part） |
| **会话结构** | 树形（id/parentId，leafId 指针） | 线性数组 | 线性，分叉时 `Session.fork()` 复制为新 session |
| **消息模型** | 可扩展联合类型（bash、branchSummary、compactionSummary 等） | user/assistant/tool_result + 压缩摘要消息 | MessageV2：Info + Part（text/reasoning/tool/file/compaction 等） |
| **压缩触发** | 阈值（contextWindow - reserveTokens）或溢出错误 | 92% 上下文窗口 | `count >= usable`（usable = limit - reserved），可配置 `auto: false` |
| **压缩策略** | 切割点算法 + 保留最近 N tokens（约 20k） | 全量替换为 1 条摘要 + 恢复文件 | 全量替换为 user+assistant 摘要对，无显式「保留最近 N 条」 |
| **压缩摘要形态** | 结构化 markdown，迭代增量可更新 | 8 段式结构化摘要 | 5 段式（Goal/Instructions/Discoveries/Accomplished/Files） |
| **压缩后恢复** | 摘要作为 user 消息插入保留上下文开头 | 摘要 + 恢复文件组成新 messages | `filterCompacted()` 截断到摘要边界，摘要对作为上下文起点 |
| **工具输出处理** | truncateHead/truncateTail（2000 行/50KB） | 未在逆向中明确 | Truncate（2000 行/50KB）+ Prune（旧工具结果打标 cleared） |
| **长期记忆** | 无显式长期层 | CLAUDE.md + readFileState | AGENTS.md / CLAUDE.md / instruction 文件 |
| **分支能力** | 树内廉价分叉，branch summary | 未明确 | 外部 fork 为新 session |
| **溢出补救** | 溢出触发压缩后自动重试 | 压缩失败有日志与 UI 恢复 | ContextOverflow 不重试，依赖预防性检查（TODO 未完善） |
| **Provider 缓存** | sessionId 传给 Codex 等 | 未在逆向中明确 | ephemeral cache hint 给 Anthropic/Bedrock 等 |
| **扩展点** | session_before_compact / session_compact 钩子 | 未明确 | experimental.session.compacting 插件 |

### 10.2 简要对比

**相比 Pi-Mono**

- Pi-Mono 是 **树形 session + append-only JSONL**；OpenCode 是 **线性 session + SQLite 三表**。
- Pi-Mono 的压缩更强调切割点算法与「保留 recent messages」；OpenCode 更强调**压缩边界替换**。
- Pi-Mono 有显式 branch summary；OpenCode 通过 `Session.fork()` 把分支提升为新 session。

**相比 Claude Code**

- Claude Code 的中期记忆接近「全量历史压成一条摘要 + 恢复部分文件」；OpenCode 在思路上更接近它，而不是 Pi-Mono。
- Claude Code 的压缩前后更依赖 readFileState；OpenCode 更依赖 `MessageV2 parts` 自己就已记录大量文件/工具上下文。
- Claude Code 的上下文溢出补救链路更完整；OpenCode 当前更依赖预防性检查。

**三者谱系**

- **Pi-Mono**：最像「带分支的会话数据库」
- **Claude Code**：最像「运行时消息数组 + 中期摘要层」
- **OpenCode**：最像「结构化事件流 + 线性压缩边界」

---

## 11. 附录：关键源码位置

核心文件如下：

- `references/opencode/packages/opencode/src/session/message-v2.ts`
  - MessageV2 定义、错误类型、toModelMessages、filterCompacted
- `references/opencode/packages/opencode/src/session/index.ts`
  - Session CRUD、SQLite 存储、fork、messages、usage 统计
- `references/opencode/packages/opencode/src/session/prompt.ts`
  - 主循环、createUserMessage、resolveTools、insertReminders、自动压缩入口
- `references/opencode/packages/opencode/src/session/processor.ts`
  - 流式消费 LLM 输出并写回 parts，finish-step 后做 usage/summary/overflow 判断
- `references/opencode/packages/opencode/src/session/compaction.ts`
  - compaction create/process、overflow 判断、prune
- `references/opencode/packages/opencode/src/session/summary.ts`
  - session diff / message diff 摘要
- `references/opencode/packages/opencode/src/session/instruction.ts`
  - AGENTS.md / CLAUDE.md / instruction 文件发现与注入
- `references/opencode/packages/opencode/src/session/llm.ts`
  - LLM 请求构造、system prompt 合并、tool repair、provider headers
- `references/opencode/packages/opencode/src/tool/truncation.ts`
  - 大工具输出截断与全量落盘
- `references/opencode/packages/opencode/src/provider/transform.ts`
  - provider 级消息归一化与缓存控制

---

## 总结

OpenCode 的消息记忆管理可以概括为一句话：

**它把一次编码会话建模成“可持久化的结构化事件流”，再用压缩边界、旧工具结果裁剪和大输出外置三层机制控制上下文规模。**

这套设计的特点是：

- 持久化结构清晰；
- 恢复逻辑统一；
- 对工具调用场景非常友好；
- 压缩策略实用且工程味很强。
