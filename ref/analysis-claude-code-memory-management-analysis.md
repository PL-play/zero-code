# Claude Code Code Agent 消息记忆管理设计与实现分析

> 基于 [analysis_claude_code](https://github.com/shareAI-lab/analysis_claude_code) 项目对 Claude Code v1.0.33 逆向分析资料的整理  
> 分析对象：混淆源码还原后的记忆与上下文管理机制  
> 分析日期：2026-03-14  
> **说明**：本仓库为逆向研究资料，非官方实现，结论仅供学习与参考。

---

## 目录

1. [项目与资料来源说明](#1-项目与资料来源说明)
2. [整体架构与记忆管理定位](#2-整体架构与记忆管理定位)
3. [消息类型与存储形态](#3-消息类型与存储形态)
4. [记忆存储机制（三层架构）](#4-记忆存储机制三层架构)
   - 4.1 [短期记忆：当前会话上下文](#41-短期记忆当前会话上下文)
   - 4.2 [中期记忆：压缩后历史](#42-中期记忆压缩后历史)
   - 4.3 [长期记忆：CLAUDE.md 与文件系统](#43-长期记忆claudemd-与文件系统)
5. [上下文压缩机制](#5-上下文压缩机制)
   - 5.1 [触发条件与阈值体系](#51-触发条件与阈值体系)
   - 5.2 [Token 计算与压缩判断](#52-token-计算与压缩判断)
   - 5.3 [压缩执行流程（wU2 / qH1）](#53-压缩执行流程wu2--qh1)
   - 5.4 [8 段式压缩提示（AU2）与摘要格式](#54-8-段式压缩提示au2与摘要格式)
   - 5.5 [压缩后文件恢复（TW5）](#55-压缩后文件恢复tw5)
6. [上下文恢复与注入](#6-上下文恢复与注入)
   - 6.1 [压缩后上下文重建](#61-压缩后上下文重建)
   - 6.2 [system-reminder 动态注入](#62-system-reminder-动态注入)
   - 6.3 [文件内容安全注入](#63-文件内容安全注入)
7. [消息队列与实时 Steering（h2A）](#7-消息队列与实时-steeringh2a)
8. [Agent 循环中的消息与压缩](#8-agent-循环中的消息与压缩)
9. [关键设计决策与启示](#9-关键设计决策与启示)
10. [与 Pi-Mono 的对比摘要](#10-与-pi-mono-的对比摘要)
11. [附录：混淆名与还原名对照](#11-附录混淆名与还原名对照)

---

## 1. 项目与资料来源说明

**analysis_claude_code** 是对 Claude Code v1.0.33 的逆向工程研究仓库，主要工作包括：

- 对约 **50,000 行混淆代码** 的分块、美化和 LLM 辅助分析；
- 还原核心组件的**混淆函数名**与执行逻辑；
- 撰写多篇技术解析与验证文档；
- 在 **Open-Claude-Code** 目录下提供部分组件的**可读复现实现**（如 h2A 消息队列）。

本报告重点关注其中与 **Code Agent 消息记忆管理** 相关的设计与实现，主要依据：

- `docs/ana_docs/memory_context_analysis.md`：记忆与上下文管理系统完整分析；
- `docs/ana_docs/memory_context_verified.md`：基于真实源码的验证与纠正；
- `docs/ana_docs/agent_loop_deep_analysis.md`：Agent 主循环与压缩调用关系；
- `docs/实时Steering机制还原代码实现.md`：h2A 队列与 Steering 机制；
- `docs/Open-Claude-Code/`：消息队列等 TypeScript 复现代码。

文档中的「还原名称」与「混淆名称」对应关系见附录。

---

## 2. 整体架构与记忆管理定位

Claude Code Agent 采用分层架构，记忆管理分布在**存储与持久化层**和**核心调度层**：

```
┌─────────────────────────────────────────────────────────────────┐
│                      Agent 核心调度层                            │
│  ┌─────────────┐         ┌─────────────┐                        │
│  │ nO 主循环   │◄────────┤ h2A 消息队列 │  ← 实时 Steering 输入  │
│  └──────┬──────┘         └─────────────┘                        │
│         │                                                        │
│         ▼ 每轮前检查                                              │
│  ┌─────────────┐         ┌─────────────┐                        │
│  │ wU2 压缩器  │         │ wu 会话流   │                        │
│  │ (阈值/执行) │         │ 生成器      │                        │
│  └─────────────┘         └─────────────┘                        │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    存储与持久化层                                │
│  短期: messages[]  中期: 压缩摘要  长期: CLAUDE.md  状态缓存     │
└─────────────────────────────────────────────────────────────────┘
```

- **nO（agentMainLoop）**：每轮开始前调用 **wU2** 做压缩判断与执行，再调用 **wu** 生成会话流。
- **h2A**：异步消息队列，用于用户输入与 Steering 的实时注入，不直接负责持久化。
- **wU2**：协调「是否压缩」与「执行压缩」，压缩后替换当前上下文，再交给主循环使用。

因此，**记忆管理** = 短期消息数组 + 阈值驱动的 wU2 压缩 + 压缩摘要作为中期记忆 + CLAUDE.md 与文件状态作为长期记忆。

---

## 3. 消息类型与存储形态

逆向分析中可见的消息相关形态包括：

- **按角色**：`user`、`assistant`、`tool_result`；
- **按用途**：普通对话消息、工具调用/结果、**压缩摘要消息**（带 `isCompactSummary: true`）、**元消息**（如 system-reminder，带 `isMeta: true`）；
- **按结构**：支持 `content` 为文本或块数组（如 `text`、`tool_use` 等），assistant 消息上挂 `usage`（含 `input_tokens`、`output_tokens`、缓存相关 token）。

**短期记忆**中，消息既以**数组**形式顺序存储（用于顺序遍历与 token 统计），也在部分逻辑中通过 **Map/uuid** 做索引（如通过 `parentUuid` 构建链式结构），实现「消息链」与随机访问。

---

## 4. 记忆存储机制（三层架构）

### 4.1 短期记忆：当前会话上下文

- **数据结构**：`messages[]` / `receivedMessages[]`，以及可选的 `messagesMap`、`sessionMessages`、`summaries` 等 Map 索引。
- **内容**：当前会话的全部消息（user、assistant、tool_result、压缩摘要、元消息等）。
- **访问**：O(1) 按 id 查找，O(n) 顺序遍历；**Token 统计**从数组末尾反向遍历，取最后一条有效 assistant 的 `usage`（见下节）。
- **生命周期**：随会话存在；压缩后**被替换**为「1 条压缩摘要 + 恢复的若干文件消息」。

### 4.2 中期记忆：压缩后历史

- **形态**：单条或多条「压缩摘要消息」，内容为 **8 段式结构化摘要**（见 5.4）。
- **标记**：`isCompactSummary: true`，便于识别与渲染。
- **作用**：替代被压缩掉的旧消息，保持上下文连续性；压缩后上下文 = **摘要 + 恢复的文件内容 + 后续新消息**。
- **与短期记忆关系**：压缩**不**追加到另一存储；而是**直接替换**当前 `messages`，旧消息可被写入 `messageHistory` 等历史记录供审计或恢复，但不再参与下一轮 LLM 上下文。

### 4.3 长期记忆：CLAUDE.md 与文件系统

- **CLAUDE.md**：项目级持久化文件，用于跨会话保存重要项目信息、偏好、历史决策等。
- **文件状态（readFileState）**：记录会话中读取过的文件及其时间戳等，压缩前会**备份**，压缩后根据策略**选择性恢复**（见 TW5）。
- **限制**（逆向得到的常量）：  
  - 恢复文件数上限 **qW5 = 20**；  
  - 单文件 token 上限 **LW5 = 8192**；  
  - 恢复总 token 上限 **MW5 = 32768**。

---

## 5. 上下文压缩机制

### 5.1 触发条件与阈值体系

- **自动压缩开关**：由配置项 `autoCompactEnabled`（还原前如 `g11()`）控制。
- **阈值**（占上下文窗口比例）：
  - **h11 = 0.92**：达到 92% 即触发自动压缩；
  - **_W5 = 0.6**：60% 为警告；
  - **jW5 = 0.8**：80% 为错误级提示。
- **逻辑**：先取当前上下文 token 使用量，再与「上下文上限 × 0.92」比较，超过则在本轮调用 LLM 前执行压缩（由 wU2 协调）。

### 5.2 Token 计算与压缩判断

**VE（还原：calculateLatestTokenUsage）**

- 从 `messages` 数组**末尾向前**遍历；
- 对每条消息尝试用 **HY5** 提取 `usage`（仅对真实 assistant 且非 synthetic、且非占位错误消息）；
- 得到第一条有效 `usage` 后，用 **zY5** 计算总 token，并返回。

**zY5（还原：sumTokensWithCache）**

- `input_tokens + (cache_creation_input_tokens ?? 0) + (cache_read_input_tokens ?? 0) + output_tokens`
- 即包含 **Prompt Caching** 的读写与创建，与 Claude API 的缓存计费一致。

**yW5（还原：shouldTriggerCompaction）**

- 若未开启自动压缩则返回 false；
- 否则用 VE 得到当前 token 数，再通过 **m11** 计算是否超过 92% 阈值（`isAboveAutoCompactThreshold`），返回该布尔值。

这样，**压缩判断**完全基于「最近一次 assistant 的 usage」，无需单独调用 tokenizer，与 Pi-Mono 的「最后一次 usage + 尾部启发式」思路不同，这里更依赖 API 返回的 usage。

### 5.3 压缩执行流程（wU2 / qH1）

**wU2（messageCompactor / executeCompressionIfNeeded）**

- 入参：当前 `messages`、执行上下文（含权限、UI 回调、abort 等）。
- 先调 **yW5** 判断是否需要压缩；若否，直接返回 `{ messages, wasCompacted: false }`。
- 若是，则调用 **qH1** 执行压缩；成功则返回 `{ messages: messagesAfterCompacting, wasCompacted: true }`。
- 异常时根据错误类型处理，必要时保持原 `messages` 并返回 `wasCompacted: false`。

**qH1（performContextCompression）**

1. **校验**：消息数组非空。
2. **统计**：VE 取当前 token 数，Re1 做消息统计，HU2 做上下文分析（用于打点）。
3. **打点**：如 `tengu_compact`，记录压缩前 token 等。
4. **UI**：设置「正在压缩」等状态（如 setStreamMode、setSpinnerMessage）。
5. **生成压缩提示**：**AU2** 生成 8 段式提示（可带自定义指令），并包装成一条 user 消息。
6. **调用 LLM**：**wu** 以「当前全部消息 + 压缩提示」为输入，使用**压缩专用模型**（如 J7()），`maxOutputTokensOverride: CU2`（16384），流式消费响应。
7. **校验输出**：必须得到有效摘要文本；若以 API 错误或「prompt too long」等前缀开头，则记录 `tengu_compact_failed` 并抛错。
8. **文件状态**：备份 `readFileState`，清空当前，再通过 **TW5** 按策略恢复部分文件（见 5.5）。
9. **待办**：PW5 取当前 agent 的 todo，若有则加入恢复列表。
10. **新 messages**：`[ 格式化后的压缩摘要消息, ...恢复的文件消息 ]`。
11. **写回**：若存在 `setMessages`，则用新数组替换；若有 `setMessageHistory`，可将本轮压缩前的消息追加到历史。
12. **恢复 UI**：清除「压缩中」状态。

压缩是**全量替换**：不保留「摘要 + 最近 N 条原始消息」的混合结构，而是「一条摘要 + 恢复的文件」作为新的上下文起点。

### 5.4 8 段式压缩提示（AU2）与摘要格式

**AU2（generateCompressionPrompt）** 不是压缩算法本身，而是**压缩提示模板生成器**：

- 输入：可选的「附加指令」字符串（如用户自定义的 compact 要求）。
- 输出：一段完整的 system/instruction 文本，要求模型按固定结构输出摘要。

**8 段式结构**（摘要需包含）：

1. Primary Request and Intent  
2. Key Technical Concepts  
3. Files and Code Sections（含文件名、代码片段、修改原因）  
4. Errors and fixes  
5. Problem Solving  
6. All user messages（非 tool 结果的用户消息）  
7. Pending Tasks  
8. Current Work  
9. Optional Next Step（与最近工作直接相关，并引用原文）

模板还要求模型先在 `<analysis>` 中做思考，再在 `<summary>` 中按上述 9 点输出；并支持通过「附加指令」注入项目特定的摘要偏好（如侧重 TypeScript、测试输出等）。

**BU2** 负责将 LLM 输出的摘要文本格式化为前端/上下文可用的消息内容，并标记 `isCompactSummary: true`。

### 5.5 压缩后文件恢复（TW5）

**TW5（postCompactFileRestore）** 在压缩完成后执行，用于把「重要文件」重新注入上下文：

- **输入**：备份的 `readFileState`（文件名 → 元数据）、执行上下文、以及最大恢复数量 **qW5 = 20**。
- **过滤**：排除与当前 agent 无关的路径（如 SW5 过滤），按时间戳降序排序，取前 20 个文件。
- **读取**：对每个文件调用 **Le1**（带 `fileReadingLimits.maxTokens: LW5`，即 8192）读内容，包装成工具结果格式（如 Nu）。
- **Token 预算**：用 **AE** 估算每个结果的 token，累加直到总 token 不超过 **MW5 = 32768**，超出则不再加入。
- **输出**：工具结果消息数组，会与压缩摘要一起组成新的 `messages`。

这样既保留「对话摘要」，又保留「最近阅读/编辑过的文件」的片段，避免丢失关键代码上下文。

---

## 6. 上下文恢复与注入

### 6.1 压缩后上下文重建

- 压缩完成后，**当前上下文** = `[ 压缩摘要消息, ...恢复的文件消息 ]`。
- 后续用户与 assistant 的新消息**追加**在该数组后。
- 不区分「摘要」与「保留的原始消息」两条路径；所有历史都通过「一条摘要 + 文件」代表，由主循环（nO）在下一轮将新的 `messages` 交给 wu 生成会话流。

### 6.2 system-reminder 动态注入

**Ie1** 负责把「当前上下文信息」组装成一条 **system-reminder** 消息并插入到消息数组前面：

- 输入：当前消息数组 A，以及键值对形式的上下文 B（如目录结构、git 状态、claudeMd 等）。
- 若 B 为空，直接返回 A。
- 否则先通过 **CY5** 做上下文大小统计（可打点 `tengu_context_size`），然后构造一条内容为 `<system-reminder>...</system-reminder>` 的消息，标明「以下为可用上下文，可能与任务无关，仅在高度相关时使用」，并设置 **isMeta: true**，再返回 `[ 该消息, ...A ]`。

这样，模型在每轮都能看到「当前项目/环境摘要」，同时被提醒不要过度依赖无关上下文。

### 6.3 文件内容安全注入

- 文件读取结果在注入到对话时，会与一段**安全提醒**（如 tG5）一起组成 content 数组：`[ 文件内容, "<system-reminder>...恶意代码检查与拒绝改进...</system-reminder>" ]`。
- 逆向文档中还提到文件类型校验、内容扫描、权限与大小限制等，与「记忆管理」交叉的是：注入的**形态**和**长度**受控，便于控制 token 与安全策略。

---

## 7. 消息队列与实时 Steering（h2A）

**h2A** 是「实时 Steering」的基础设施：用户在新一轮响应未结束前就可以发送下一条输入，该输入通过队列被主循环消费，实现**打断/转向**。

- **接口**：实现 **AsyncIterable**，支持 `for await (const msg of queue)`；生产者通过 **enqueue** 投递消息，通过 **done()** / **error()** 结束或报错。
- **零延迟路径**：若当前有**正在等待的读取者**（next() 返回的 Promise 尚未 resolve），则 enqueue 时**不放入缓冲区**，而是直接 resolve 该 Promise 并传入本条消息，实现「有读者就立刻送达」。
- **缓冲路径**：若无等待的读者，则消息进入**主缓冲区**（如循环队列）；后续 next() 先消费缓冲区，空再挂起等待。
- **背压**：缓冲区满时可配置策略（丢弃最旧、丢弃最新、报错、阻塞等）；Open-Claude-Code 复现中采用循环缓冲区与 DROP_OLDEST 等策略。
- **单次迭代**：队列只能被迭代一次（started 标志），避免多个消费者同时消费同一队列。

因此，**记忆管理**负责「当前要发给 LLM 的 messages」；**h2A** 负责「用户/系统何时注入新消息」，二者配合实现「边跑边收新指令」的 Steering，而不改变持久化与压缩的职责边界。

---

## 8. Agent 循环中的消息与压缩

- **nO（agentMainLoop）** 在**每轮开始**时：
  - 先对当前 `messages` 调用 **wU2**；
  - 若返回 `wasCompacted: true`，则用压缩后的 `messages` 作为本轮的「原始消息」，并可选地更新 turnState、打点；
  - 再基于**该系统提示 + 消息**调用 **wu** 生成会话流，处理工具调用与流式输出。
- 压缩发生在**调用 LLM 之前**，而不是在溢出错误之后；即**预防式压缩**（92% 即压），与 Pi-Mono 的「阈值 + 溢出双触发」不同。
- 消息流：用户输入 →（可选）经 h2A 入队 → 主循环取出 → 与当前 messages 一起做压缩检查 → 若压缩则替换 messages → wu 用新 messages 调 API → 工具调用/流式输出 → 下一轮。

---

## 9. 关键设计决策与启示

1. **三层记忆**：短期（当前 messages）、中期（压缩摘要）、长期（CLAUDE.md + 文件状态），职责清晰，压缩只替换短期，不混合「摘要+原始」在同一上下文窗口。
2. **92% 预防式压缩**：在接近满窗之前就压缩，减少溢出发生；代价是压缩频率可能较高，依赖 8 段式摘要质量。
3. **8 段式结构化摘要**：通过固定模板（AU2）约束输出格式，便于解析、存储和后续检索；同时支持自定义指令适配项目。
4. **压缩后文件恢复**：不只保留摘要，还按时间戳与 token 预算恢复部分文件内容，适合代码场景的「最近文件」重要性。
5. **Token 计算完全基于 usage**：从最后一条 assistant 的 usage 反推当前上下文长度，无需本地 tokenizer，但依赖 API 返回准确。
6. **专用压缩模型**：压缩请求使用独立模型与 maxOutputTokens（如 16384），可与主对话模型分离，便于成本与质量权衡。
7. **system-reminder 注入**：把环境/项目信息以「元消息」形式注入，并明确标注「可能不相关」，兼顾信息可用性与避免模型过度依赖。
8. **h2A 零延迟路径**：有读者时直接交付消息，减少延迟，适合实时交互与 Steering；与「记忆管理」解耦，只负责消息何时被主循环消费。

---

## 10. 与 Pi-Mono 的对比摘要

| 维度           | Claude Code（本分析）     | Pi-Mono                    |
|----------------|---------------------------|----------------------------|
| 存储形态       | 数组 + 可选 Map，无 JSONL | JSONL 追加 + 树形 id/parentId |
| 压缩触发       | 92% 预防式               | 阈值 + 溢出双触发，可自动重试 |
| 压缩策略       | 全量替换为 1 条摘要 + 文件恢复 | 摘要 + 保留最近 N 条（keepRecentTokens） |
| 摘要格式       | 8 段式固定模板（AU2）    | 结构化 Markdown，可迭代增量 |
| Token 估算     | 仅用最后 assistant usage | 最后 usage + 尾部启发式   |
| 长期记忆       | CLAUDE.md + readFileState | 会话文件 + 分支/树         |
| 消息队列       | h2A 双缓冲/零延迟        | steer/followUp 队列        |
| 分支/多会话   | 未在记忆文档中展开       | 树形分支、branch、fork    |

---

## 11. 附录：混淆名与还原名对照

| 混淆名 | 还原名 / 含义 |
|--------|----------------|
| nO     | agentMainLoop，主循环 |
| wu     | conversationStreamGenerator，会话流生成器 |
| nE2    | dialogPipelineProcessor，对话管道 |
| wU2    | messageCompactor / executeCompressionIfNeeded |
| qH1    | performContextCompression，执行压缩 |
| AU2    | generateCompactionPrompt，8 段式压缩提示生成 |
| BU2    | 格式化压缩摘要为消息内容 |
| VE     | calculateLatestTokenUsage |
| HY5    | extractAssistantUsage |
| zY5    | sumTokensWithCache |
| yW5    | shouldTriggerCompaction |
| m11    | 阈值与百分比计算（percentLeft, isAboveWarningThreshold 等） |
| g11    | isAutoCompactEnabled |
| TW5    | postCompactFileRestore |
| Ie1    | 组装 system-reminder 并注入消息数组 |
| CY5    | 上下文大小统计/打点 |
| h2A    | 异步消息队列（AsyncMessageQueue） |
| J7     | 压缩专用模型获取 |
| CU2    | 16384（压缩输出 max tokens） |
| qW5    | 20（恢复文件数上限） |
| LW5    | 8192（单文件 token 上限） |
| MW5    | 32768（恢复总 token 上限） |
| h11    | 0.92（自动压缩阈值） |
| _W5    | 0.6（警告阈值） |
| jW5    | 0.8（错误阈值） |

---

## 参考文献（项目内文档）

- `docs/ana_docs/memory_context_analysis.md`
- `docs/ana_docs/memory_context_verified.md`
- `docs/ana_docs/agent_loop_deep_analysis.md`
- `docs/实时Steering机制还原代码实现.md`
- `docs/Open-Claude-Code/src/core/message-queue.ts`、`message-queue.md`
- `Claude_Code_Agent系统完整技术解析.md`
