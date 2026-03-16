# Pi-Mono Code Agent 消息记忆管理设计与实现分析

> 基于 [pi-mono](https://github.com/badlogic/pi-mono) 项目源码的深度分析  
> 分析日期：2026-03-14

---

## 目录

1. [项目架构概览](#1-项目架构概览)
2. [消息类型体系](#2-消息类型体系)
3. [记忆存储机制](#3-记忆存储机制)
   - 3.1 [会话文件格式（JSONL 追加日志）](#31-会话文件格式jsonl-追加日志)
   - 3.2 [树形会话结构](#32-树形会话结构)
   - 3.3 [SessionManager：核心存储引擎](#33-sessionmanager核心存储引擎)
4. [上下文压缩机制（Compaction）](#4-上下文压缩机制compaction)
   - 4.1 [触发条件](#41-触发条件)
   - 4.2 [切割点算法](#42-切割点算法)
   - 4.3 [摘要生成策略](#43-摘要生成策略)
   - 4.4 [迭代增量压缩](#44-迭代增量压缩)
   - 4.5 [文件操作追踪](#45-文件操作追踪)
   - 4.6 [扩展系统集成](#46-扩展系统集成)
5. [上下文恢复机制](#5-上下文恢复机制)
   - 5.1 [从树路径构建上下文](#51-从树路径构建上下文)
   - 5.2 [压缩后的上下文重建](#52-压缩后的上下文重建)
   - 5.3 [会话切换与恢复](#53-会话切换与恢复)
6. [分支摘要（Branch Summarization）](#6-分支摘要branch-summarization)
7. [上下文溢出处理与自动重试](#7-上下文溢出处理与自动重试)
8. [消息到 LLM 的转换管道](#8-消息到-llm-的转换管道)
9. [Agent 循环中的消息流转](#9-agent-循环中的消息流转)
10. [关键设计决策与启示](#10-关键设计决策与启示)
11. [补充：容易忽略的关键细节](#11-补充容易忽略的关键细节)
    - 11.1 [工具输出截断——压缩前的第一道防线](#111-工具输出截断压缩前的第一道防线)
    - 11.2 [混合 Token 估算策略](#112-混合-token-估算策略)
    - 11.3 [Provider 级别的 Session 缓存](#113-provider-级别的-session-缓存)
    - 11.4 [两类自定义条目——元数据 vs 上下文](#114-两类自定义条目元数据-vs-上下文)
    - 11.5 [Prompt 前的预防性压缩检查](#115-prompt-前的预防性压缩检查)
    - 11.6 [压缩后的消息队列恢复](#116-压缩后的消息队列恢复)
    - 11.7 [双层设置管理与压缩配置](#117-双层设置管理与压缩配置)
    - 11.8 [自定义压缩模型——扩展示例](#118-自定义压缩模型扩展示例)
    - 11.9 [溢出检测的安全守卫](#119-溢出检测的安全守卫)
    - 11.10 [图片的 Token 估算](#1110-图片的-token-估算)

---

## 1. 项目架构概览

Pi-Mono 是一个用于构建 AI Agent 的 TypeScript monorepo，包含以下核心包：

| 包名 | 职责 |
|------|------|
| `@mariozechner/pi-ai` | 统一的多 provider LLM API（OpenAI、Anthropic、Google 等） |
| `@mariozechner/pi-agent-core` | Agent 运行时，工具调用与状态管理 |
| `@mariozechner/pi-coding-agent` | 交互式编码 Agent CLI |

在记忆管理方面，职责分层如下：

```
┌───────────────────────────────────────────────────┐
│          coding-agent (AgentSession)               │
│  会话管理、压缩策略、分支摘要、自动重试            │
├───────────────────────────────────────────────────┤
│          agent-core (Agent + AgentLoop)            │
│  消息状态机、LLM 调用循环、上下文变换钩子          │
├───────────────────────────────────────────────────┤
│          pi-ai (streamSimple / completeSimple)     │
│  底层 LLM 流式调用、Token 统计                    │
└───────────────────────────────────────────────────┘
```

核心设计理念：**Agent-core 层不关心持久化和压缩策略，只提供 `transformContext` 和 `convertToLlm` 两个钩子**。具体的记忆管理策略全部在 coding-agent 层实现。

---

## 2. 消息类型体系

Pi-Mono 采用**可扩展的联合类型**设计消息体系。基础类型来自 `pi-ai`，自定义类型通过 TypeScript 声明合并注入：

### 基础 LLM 消息

```typescript
type Message = UserMessage | AssistantMessage | ToolResultMessage;
```

### 自定义 Agent 消息（coding-agent 层扩展）

```typescript
// 通过声明合并扩展 AgentMessage 联合类型
interface CustomAgentMessages {
  bashExecution: BashExecutionMessage;  // bash 命令执行结果
  custom: CustomMessage;                // 扩展注入的自定义消息
  branchSummary: BranchSummaryMessage;  // 分支摘要
  compactionSummary: CompactionSummaryMessage; // 压缩摘要
}
```

最终的 `AgentMessage` 是所有类型的联合：

```typescript
type AgentMessage = Message | CustomAgentMessages[keyof CustomAgentMessages];
```

这一设计允许**内存中存储丰富的语义信息**（bash 结果、分支摘要等），同时通过 `convertToLlm()` 转换管道在发送给 LLM 之前统一转化为标准 `Message[]` 格式。

---

## 3. 记忆存储机制

### 3.1 会话文件格式（JSONL 追加日志）

会话以 **JSONL（JSON Lines）** 格式存储，每行一个 JSON 对象，采用**追加写入（append-only）**策略：

```
{"type":"session","version":3,"id":"uuid","timestamp":"...","cwd":"/path"}
{"type":"thinking_level_change","id":"a1b2","parentId":null,"thinkingLevel":"medium",...}
{"type":"message","id":"c3d4","parentId":"a1b2","message":{"role":"user",...},...}
{"type":"message","id":"e5f6","parentId":"c3d4","message":{"role":"assistant",...},...}
{"type":"compaction","id":"g7h8","parentId":"e5f6","summary":"...","firstKeptEntryId":"c3d4",...}
```

**关键特性：**

- **延迟写入（Lazy Flush）**：在第一条 assistant 消息到达前，所有条目只保留在内存中。这避免了对话未完成时创建大量空/无用的会话文件。
- **版本迁移**：支持 v1 → v2（添加树形 id/parentId）→ v3（重命名 hookMessage 为 custom）的渐进式迁移。
- **文件位置**：`~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl`

### 3.2 树形会话结构

**这是 Pi-Mono 最精妙的设计之一。** 每个条目都有 `id` 和 `parentId`，形成一棵有向树：

```
           [user msg: "实现功能A"]
              /                 \
  [assistant: "方案1"]    [assistant: "方案2"]  (分支)
       |                        |
  [user: "修改"]           [user: "继续"]
       |                        |
  [assistant: ...]         [compaction]
                                |
                           [user: "新需求"]
```

- **叶节点指针（leafId）**：标识当前对话位置
- **分支（branch）**：修改 `leafId` 即创建新分支，**不修改或删除任何历史条目**
- **路径遍历**：从 `leafId` 沿 `parentId` 回溯到根，获取当前分支的完整路径

这种设计实现了：
- **无损编辑**：所有历史分支都完整保留
- **廉价分叉**：只需移动 leafId
- **选择性上下文**：只有当前路径上的消息参与 LLM 上下文

### 3.3 SessionManager：核心存储引擎

`SessionManager` 是所有持久化操作的中枢，核心接口包括：

| 方法 | 功能 |
|------|------|
| `appendMessage()` | 追加消息条目到当前叶节点 |
| `appendCompaction()` | 追加压缩摘要条目 |
| `appendCustomEntry()` | 追加扩展自定义条目（不参与 LLM 上下文） |
| `appendCustomMessageEntry()` | 追加自定义消息条目（参与 LLM 上下文） |
| `branch(id)` | 移动叶指针到指定条目（创建分支） |
| `branchWithSummary()` | 带摘要的分支（保留被遗弃分支的上下文） |
| `buildSessionContext()` | 构建当前路径的 LLM 上下文 |
| `getBranch()` | 获取当前路径上所有条目 |
| `getTree()` | 获取完整树结构 |
| `createBranchedSession()` | 将一个分支提取为独立的会话文件 |

**创建模式**：

```typescript
SessionManager.create(cwd)           // 新建会话
SessionManager.open(path)            // 打开指定文件
SessionManager.continueRecent(cwd)   // 续接最近会话
SessionManager.inMemory()            // 纯内存模式（不持久化）
SessionManager.forkFrom(source, cwd) // 从其他项目 fork
```

---

## 4. 上下文压缩机制（Compaction）

上下文压缩是 Pi-Mono 记忆管理的核心能力，代码位于 `packages/coding-agent/src/core/compaction/` 目录。

### 4.1 触发条件

压缩有两种触发路径：

#### 阈值触发（Threshold）

```typescript
function shouldCompact(contextTokens, contextWindow, settings): boolean {
  return contextTokens > contextWindow - settings.reserveTokens;
}
```

当上下文 token 数超过 `contextWindow - reserveTokens` 时触发。默认 `reserveTokens = 16384`。

#### 溢出触发（Overflow）

当 LLM 返回上下文溢出错误时，自动触发紧急压缩。此时：
1. 从 agent 状态中移除错误消息
2. 执行压缩
3. **自动重试**之前失败的请求

两种触发路径的核心区别：
- **阈值触发**：压缩后不自动重试，用户手动继续
- **溢出触发**：压缩后自动重试，对用户透明

### 4.2 切割点算法

切割点算法决定哪些消息被压缩、哪些被保留：

```
[所有消息]
├── [被压缩的消息] → 生成摘要
├── [切割点] ← firstKeptEntryId
└── [保留的消息] → 原样保留
```

算法步骤（`findCutPoint` 函数）：

1. **从最新消息向前遍历**，累计估算 token 数
2. **当累计 token 超过 `keepRecentTokens`（默认 20000）时停止**
3. 在停止位置找到**最近的有效切割点**

**有效切割点规则**：
- **允许切割**：user、assistant、custom、bashExecution、branchSummary、compactionSummary 消息
- **禁止切割**：toolResult 消息（必须跟随其 toolCall）
- **特殊处理**：如果切割点落在一个 turn 的中间（在 assistant 而非 user 消息处），则标记为 `isSplitTurn`，需要额外生成 turn 前缀摘要

**Token 估算**使用简单的字符/4 启发式：

```typescript
function estimateTokens(message: AgentMessage): number {
  // 累计文本字符数
  return Math.ceil(chars / 4);
}
```

### 4.3 摘要生成策略

压缩使用 LLM 自身生成结构化摘要，摘要格式是预定义的 Markdown 模板：

```markdown
## Goal
[用户试图完成什么？]

## Constraints & Preferences
- [约束和偏好]

## Progress
### Done
- [x] [已完成任务]
### In Progress
- [ ] [进行中任务]
### Blocked
- [阻塞问题]

## Key Decisions
- **[决策]**: [原因]

## Next Steps
1. [下一步计划]

## Critical Context
- [关键数据和引用]
```

当切割发生在 turn 中间时（`isSplitTurn`），会**并行生成两个摘要**：

```typescript
const [historyResult, turnPrefixResult] = await Promise.all([
  generateSummary(messagesToSummarize, ...),        // 历史摘要
  generateTurnPrefixSummary(turnPrefixMessages, ...) // Turn 前缀摘要
]);
summary = `${historyResult}\n\n---\n\n**Turn Context (split turn):**\n\n${turnPrefixResult}`;
```

Turn 前缀摘要使用更简洁的格式：

```markdown
## Original Request
[用户在这个 turn 中要求什么？]
## Early Progress
- [前缀中的关键决策和工作]
## Context for Suffix
- [理解保留部分所需的信息]
```

### 4.4 迭代增量压缩

**这是一个关键的设计决策。** 当会话已经经历过压缩时，后续压缩不是从头生成全新摘要，而是基于**上一次压缩的摘要进行增量更新**：

```typescript
// 获取上一次压缩的摘要
let previousSummary: string | undefined;
if (prevCompactionIndex >= 0) {
  const prevCompaction = pathEntries[prevCompactionIndex] as CompactionEntry;
  previousSummary = prevCompaction.summary;
}
```

增量更新使用专门的 prompt：

> "The messages above are NEW conversation messages to incorporate into the existing summary provided in `<previous-summary>` tags."
>
> 规则：
> - **PRESERVE** 已有信息
> - **ADD** 新进展、决策和上下文
> - **UPDATE** 进度状态（In Progress → Done）
> - **PRESERVE** 精确的文件路径、函数名和错误消息

这种设计的优势：
- 避免信息在多次压缩中逐渐丢失
- 降低每次压缩的计算成本（只需处理新增消息）
- 保持摘要格式的一致性

### 4.5 文件操作追踪

压缩过程会追踪文件操作，确保 LLM 知道哪些文件被读取/修改过：

```typescript
interface FileOperations {
  read: Set<string>;     // 读取的文件
  written: Set<string>;  // 写入的文件
  edited: Set<string>;   // 编辑的文件
}
```

追踪来源：
1. **工具调用**：从 assistant 消息的 toolCall 中提取 `read`/`write`/`edit` 操作
2. **历史压缩**：从上一次 CompactionEntry 的 details 中继承

最终附加到摘要末尾：

```xml
<read-files>
src/index.ts
src/utils.ts
</read-files>

<modified-files>
src/main.ts
src/config.ts
</modified-files>
```

### 4.6 扩展系统集成

压缩系统通过事件钩子与扩展系统深度集成：

```
session_before_compact → 扩展可以：
  ├── cancel: true     → 取消压缩
  └── compaction: {...} → 提供自定义压缩结果

session_compact → 压缩完成后通知扩展
```

扩展可以完全接管压缩逻辑（例如实现基于 artifact 的结构化压缩），只需返回符合 `CompactionResult` 接口的结果。

---

## 5. 上下文恢复机制

### 5.1 从树路径构建上下文

`buildSessionContext()` 是上下文恢复的核心函数。它从 `leafId` 沿 `parentId` 回溯到根，收集路径上的所有条目：

```typescript
function buildSessionContext(entries, leafId, byId): SessionContext {
  // 1. 从 leaf 回溯到 root，收集路径
  const path: SessionEntry[] = [];
  let current = byId.get(leafId);
  while (current) {
    path.unshift(current);
    current = current.parentId ? byId.get(current.parentId) : undefined;
  }

  // 2. 提取设置（thinking level、model）
  // 3. 查找最新的 compaction 条目
  // 4. 根据是否有 compaction，构建消息列表
  return { messages, thinkingLevel, model };
}
```

### 5.2 压缩后的上下文重建

当路径中存在 CompactionEntry 时，上下文构建遵循特殊规则：

```
[路径中的所有条目]
├── [compaction 之前、firstKeptEntryId 之前的条目] → 忽略（已被摘要替代）
├── [CompactionEntry] → 转化为 CompactionSummaryMessage（作为上下文开头）
├── [firstKeptEntryId 到 compaction 之间的条目] → 原样保留
└── [compaction 之后的条目] → 原样保留
```

具体代码逻辑：

```typescript
if (compaction) {
  // 1. 先发出摘要
  messages.push(createCompactionSummaryMessage(compaction.summary, ...));

  // 2. 发出 firstKeptEntryId 到 compaction 之间的"保留消息"
  for (let i = 0; i < compactionIdx; i++) {
    if (entry.id === compaction.firstKeptEntryId) foundFirstKept = true;
    if (foundFirstKept) appendMessage(entry);
  }

  // 3. 发出 compaction 之后的消息
  for (let i = compactionIdx + 1; i < path.length; i++) {
    appendMessage(entry);
  }
}
```

压缩摘要在发送给 LLM 时被包裹为用户消息：

```
The conversation history before this point was compacted into the following summary:

<summary>
[压缩摘要内容]
</summary>
```

### 5.3 会话切换与恢复

`AgentSession.switchSession()` 处理完整的会话恢复流程：

```
1. 断开 Agent 事件监听
2. 中止当前操作
3. 清空排队的消息
4. SessionManager.setSessionFile(path) → 加载 JSONL 文件
5. 文件版本迁移（如需要）
6. buildSessionContext() → 构建完整上下文
7. agent.replaceMessages(context.messages)
8. 恢复模型选择（从 ModelChangeEntry）
9. 恢复 thinking level（从 ThinkingLevelChangeEntry）
10. 重新连接 Agent 事件监听
```

---

## 6. 分支摘要（Branch Summarization）

当用户在会话树中导航到不同分支时，可以为被遗弃的分支生成摘要，避免上下文丢失。

### 工作流程

```
用户在分支 A 的叶节点 → 想导航到分支 B 的节点
                                     ↓
collectEntriesForBranchSummary(oldLeafId, targetId)
  → 找到两条路径的共同祖先
  → 收集从 oldLeaf 到共同祖先之间的条目
                                     ↓
prepareBranchEntries(entries, tokenBudget)
  → 从最新到最旧遍历，在 token 预算内收集消息
  → 优先保留最近的上下文
  → 摘要类型条目（compaction/branch_summary）享有优先权
                                     ↓
generateBranchSummary(entries, options)
  → 序列化为文本 → LLM 摘要
  → 附加文件操作列表
                                     ↓
sessionManager.branchWithSummary(newLeafId, summary)
  → 在目标位置创建 BranchSummaryEntry
```

分支摘要在 LLM 上下文中呈现为：

```
The following is a summary of a branch that this conversation came back from:

<summary>
[分支摘要内容]
</summary>
```

### 与 Compaction 的区别

| 特性 | Compaction | Branch Summary |
|------|-----------|----------------|
| 目的 | 减少上下文 token | 保留导航时的上下文 |
| 触发 | 自动（阈值/溢出）或手动 | 用户导航树节点时 |
| 消息去向 | 被替换为摘要 | 附加为额外上下文 |
| 支持增量 | 是（基于 previousSummary） | 否 |
| 存储类型 | CompactionEntry | BranchSummaryEntry |

---

## 7. 上下文溢出处理与自动重试

Pi-Mono 实现了两层自动恢复机制：

### 上下文溢出恢复

```
LLM 返回溢出错误
  → 检测到 isContextOverflow
  → 从 agent 状态移除错误消息
  → 触发自动压缩（overflow 模式）
  → 压缩成功后自动 continue()
```

溢出检测有安全守卫：
- **模型一致性检查**：只在错误来自当前模型时触发（避免切换到更大上下文模型后误触发）
- **压缩后检查**：忽略在最新压缩之前产生的错误

### 可重试错误恢复

```typescript
// 匹配的错误模式
/overloaded|rate.?limit|too many requests|429|500|502|503|504|
 service.?unavailable|server error|fetch failed|retry delay/i
```

策略：**指数退避重试**

```typescript
const delayMs = settings.baseDelayMs * 2 ** (retryAttempt - 1);
```

- 从 agent 状态移除错误消息（保留在会话文件中供审计）
- 等待退避延迟（可中止）
- 重试 `agent.continue()`
- 成功后立即重置计数器

---

## 8. 消息到 LLM 的转换管道

从内存中的 `AgentMessage[]` 到发送给 LLM 的 `Message[]`，经过两级转换：

```
AgentMessage[]
    │
    ▼ transformContext (可选，AgentMessage[] → AgentMessage[])
    │  用于上下文剪裁、注入外部上下文
    │
AgentMessage[] (变换后)
    │
    ▼ convertToLlm (AgentMessage[] → Message[])
    │  处理自定义消息类型转换
    │
Message[] (LLM 兼容格式)
    │
    ▼ 发送给 LLM
```

`convertToLlm` 的转换规则：

| AgentMessage 类型 | LLM Message 类型 | 规则 |
|-------------------|------------------|------|
| `user` | `user` | 直接传递 |
| `assistant` | `assistant` | 直接传递 |
| `toolResult` | `toolResult` | 直接传递 |
| `bashExecution` | `user` | 格式化命令和输出为文本；`excludeFromContext` 时过滤 |
| `custom` | `user` | content 转为用户消息 |
| `compactionSummary` | `user` | 包裹在 `<summary>` 标签中 |
| `branchSummary` | `user` | 包裹在 `<summary>` 标签中 |

---

## 9. Agent 循环中的消息流转

Agent 循环（`agent-loop.ts`）是消息流转的执行引擎：

```
用户输入
  │
  ▼
agentLoop(prompts, context, config)
  │
  ├── 外层循环：处理 follow-up 消息
  │     │
  │     ├── 内层循环：处理 tool calls 和 steering 消息
  │     │     │
  │     │     ├── 检查 pending 消息 → 注入到上下文
  │     │     │
  │     │     ├── streamAssistantResponse()
  │     │     │     ├── transformContext() ← 上下文变换钩子
  │     │     │     ├── convertToLlm()    ← 消息格式转换
  │     │     │     └── LLM 流式调用
  │     │     │
  │     │     ├── 执行工具调用
  │     │     │     ├── 每个工具执行后检查 steering 消息
  │     │     │     └── 有 steering → 跳过剩余工具
  │     │     │
  │     │     └── 获取新的 steering 消息 → 继续内层循环
  │     │
  │     └── 获取 follow-up 消息 → 继续外层循环
  │
  └── agent_end 事件
        │
        ├── 检查 retryable error → 自动重试
        └── 检查 compaction → 自动压缩
```

### 三种消息注入方式

| 方式 | 时机 | 用途 |
|------|------|------|
| `steer()` | 工具执行间隙 | 中断当前操作，改变方向 |
| `followUp()` | agent 完成后 | 追加后续任务 |
| `prompt()` | 非流式时 | 发起新的对话轮次 |

---

## 10. 关键设计决策与启示

### 10.1 追加日志 + 树结构 = 无损历史

会话文件只追加不修改，分支只需移动指针。这意味着：
- **不丢失任何历史**：所有探索路径都完整保留
- **崩溃安全**：追加写入是原子的，不会破坏已有数据
- **审计友好**：完整的操作历史可供回溯

### 10.2 延迟持久化

在收到第一条 assistant 消息之前不写入文件。这避免了用户发送消息后立即退出时产生大量只有用户消息的无用会话文件。

### 10.3 压缩分离原则

压缩逻辑（何时压缩、如何切割）与存储逻辑（SessionManager）完全分离。`prepareCompaction()` 是纯函数，不产生副作用，只返回准备数据，便于测试和扩展介入。

### 10.4 增量摘要而非全量重写

多次压缩时基于上一次摘要更新，而非从头生成。这在长会话中显著减少信息损失，同时降低每次压缩的 token 消耗。

### 10.5 摘要与原始消息共存

压缩后，摘要和保留的原始消息**同时存在于上下文中**。摘要在前（提供全局背景），原始消息在后（提供精确的近期上下文）。这比纯摘要方案保留了更多细节。

### 10.6 双模式溢出处理

- **阈值模式**：主动压缩，不中断用户流程
- **溢出模式**：被动响应，压缩后自动重试

这两种模式互补，确保即使阈值检测不够准确（因为 token 估算是启发式的），溢出时也能优雅恢复。

### 10.7 扩展系统的深度集成

压缩不是黑盒操作。扩展可以：
- 拦截并取消压缩
- 提供自定义压缩结果
- 监听压缩完成事件
- 注入自定义消息到 LLM 上下文
- 追加不可见的元数据条目到会话

这为更复杂的记忆管理策略（如 artifact-based compaction、RAG 集成等）预留了接口。

### 10.8 关键配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `reserveTokens` | 16384 | 触发阈值 = contextWindow - reserveTokens |
| `keepRecentTokens` | 20000 | 压缩后保留的最近消息的估计 token 数 |
| `enabled` | true | 是否启用自动压缩 |

---

## 11. 补充：容易忽略的关键细节

### 11.1 工具输出截断——压缩前的第一道防线

在消息进入上下文之前，工具输出（read、bash、grep 等）会经过**硬截断**（`truncate.ts`）：

| 限制 | 默认值 | 说明 |
|------|--------|------|
| 行数上限 | 2000 行 | 超出部分被丢弃 |
| 字节上限 | 50KB | 超出部分被丢弃 |
| Grep 单行长度 | 500 字符 | 超长匹配行被截断 |

截断策略因工具而异：
- **文件读取**：头部截断（`truncateHead`）——保留文件开头，因为通常更重要
- **Bash 输出**：尾部截断（`truncateTail`）——保留结尾，因为错误信息和最终结果在末尾

这是一种**预防性**的 token 管理，在消息进入上下文之前就控制其大小，减轻后续压缩的压力。截断处理会正确处理 UTF-8 多字节字符边界，避免产生乱码。

### 11.2 混合 Token 估算策略

`estimateContextTokens()` 不是简单地用启发式估算所有消息，而是采用**混合策略**：

```
上下文总 token ≈ 最后一条 assistant 的实际 usage + 之后新增消息的估算 token
```

```typescript
function estimateContextTokens(messages: AgentMessage[]): ContextUsageEstimate {
  // 1. 找到最后一条有效 assistant 消息的 usage（跳过 aborted/error）
  const usageInfo = getLastAssistantUsageInfo(messages);

  if (!usageInfo) {
    // 没有 assistant 消息时，全部使用启发式估算
    return { tokens: heuristicEstimate, ... };
  }

  // 2. 使用 LLM 返回的真实 token 数
  const usageTokens = calculateContextTokens(usageInfo.usage);

  // 3. 只对 assistant 之后的新消息使用启发式估算
  let trailingTokens = 0;
  for (let i = usageInfo.index + 1; i < messages.length; i++) {
    trailingTokens += estimateTokens(messages[i]);
  }

  return { tokens: usageTokens + trailingTokens, ... };
}
```

优势：**大部分 token 使用 LLM 返回的精确数值**，只有最近几条新增消息才用启发式估算。这比全量启发式估算准确得多，同时避免了为"估算 token"单独调用 tokenizer 的成本。

### 11.3 Provider 级别的 Session 缓存

Agent 维护一个 `sessionId` 属性，传递给支持会话级缓存的 LLM provider（如 OpenAI Codex）：

```typescript
// Agent 构造时设置
agent.sessionId = sessionManager.getSessionId();

// 切换会话时同步更新
async switchSession(path) {
  this.sessionManager.setSessionFile(path);
  this.agent.sessionId = this.sessionManager.getSessionId();
}
```

这使得 LLM provider 可以在**服务端缓存**上下文 prefix，避免重复传输和处理大量相同的上下文前缀。当会话切换、分支创建或恢复时，sessionId 会同步更新。

### 11.4 两类自定义条目——元数据 vs 上下文

扩展系统提供了两种截然不同的自定义条目类型，它们的 LLM 上下文参与方式不同：

| 类型 | `CustomEntry` | `CustomMessageEntry` |
|------|--------------|---------------------|
| 用途 | 存储扩展内部状态 | 向 LLM 注入上下文 |
| 参与 LLM 上下文 | **否** | **是**（转为 user 消息） |
| TUI 显示 | 不显示 | 可配置（`display: true/false`） |
| 典型场景 | artifact 索引、版本标记 | 文件变更通知、环境信息 |
| 持久化 | 追加到 JSONL | 追加到 JSONL |

这种区分允许扩展既能持久化自己的内部状态（不污染 LLM 上下文），又能在需要时向 LLM 注入额外信息。

### 11.5 Prompt 前的预防性压缩检查

在发送新的 prompt 之前，`AgentSession.prompt()` 会主动检查上一条 assistant 消息：

```typescript
async prompt(text, options) {
  // ...
  // 检查是否需要在发送前先压缩（捕获中止的响应）
  const lastAssistant = this._findLastAssistantMessage();
  if (lastAssistant) {
    await this._checkCompaction(lastAssistant, false);
  }
  // ...
}
```

这处理了一个边界情况：用户中止（abort）了一个 assistant 响应后，`agent_end` 事件中的压缩检查会跳过 `aborted` 状态的消息。但当用户发送下一条消息时，此时上下文可能已经接近溢出。这个预防性检查确保在新的 LLM 调用之前先执行压缩。

### 11.6 压缩后的消息队列恢复

自动压缩（threshold 模式）完成后，如果 Agent 的消息队列中仍有等待的 follow-up 或 steering 消息，系统会自动启动 `agent.continue()` 来消费这些消息：

```typescript
if (willRetry) {
  // 溢出模式：自动重试
  setTimeout(() => { this.agent.continue().catch(() => {}); }, 100);
} else if (this.agent.hasQueuedMessages()) {
  // 阈值模式：有排队消息时自动恢复
  setTimeout(() => { this.agent.continue().catch(() => {}); }, 100);
}
```

这避免了一个问题：用户在 Agent 处理时排队了 follow-up 消息 → Agent 完成后触发阈值压缩 → 压缩中断了正常的消息传递循环 → 排队消息被"遗忘"。

### 11.7 双层设置管理与压缩配置

压缩和重试的所有参数都通过 `SettingsManager` 管理，支持**全局 + 项目**双层配置：

```
~/.pi/agent/settings.json         (全局设置)
<project>/.pi/settings.json       (项目设置，优先级更高)
```

项目设置覆盖全局设置（深度合并），允许不同项目使用不同的压缩策略：

```json
{
  "compaction": {
    "enabled": true,
    "reserveTokens": 16384,
    "keepRecentTokens": 20000
  },
  "retry": {
    "enabled": true,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "maxDelayMs": 60000
  }
}
```

设置修改采用**文件锁 + 异步写入队列**机制，避免并发 Agent 之间的写入冲突。

### 11.8 自定义压缩模型——扩展示例

项目提供了一个 `custom-compaction.ts` 扩展示例，展示了如何**使用更便宜的模型**（如 Gemini Flash）来执行压缩摘要：

```typescript
pi.on("session_before_compact", async (event, ctx) => {
  // 使用 Gemini Flash 代替主对话模型进行压缩
  const model = ctx.modelRegistry.find("google", "gemini-2.5-flash");
  const apiKey = await ctx.modelRegistry.getApiKey(model);

  // 合并所有待压缩消息
  const allMessages = [...messagesToSummarize, ...turnPrefixMessages];

  // 生成摘要后返回
  return {
    compaction: { summary, firstKeptEntryId, tokensBefore }
  };
});
```

这种设计允许用户在**摘要质量和成本之间做权衡**——主对话可以使用强大但昂贵的模型（如 Claude Opus），而压缩摘要使用快速便宜的模型。

### 11.9 溢出检测的安全守卫

溢出自动压缩有多层保护，防止误触发：

```typescript
// 守卫1: 模型一致性检查
const sameModel = this.model &&
  assistantMessage.provider === this.model.provider &&
  assistantMessage.model === this.model.id;

// 守卫2: 压缩后遗留错误检查
const errorIsFromBeforeCompaction =
  compactionEntry !== null &&
  assistantMessage.timestamp < new Date(compactionEntry.timestamp).getTime();

// 只有同一模型且不是压缩前的遗留错误才触发
if (sameModel && !errorIsFromBeforeCompaction && isContextOverflow(...)) {
  // 触发溢出压缩
}
```

**场景1（模型切换）**：用户从小窗口模型（如 Opus）切换到大窗口模型（如 Codex），旧模型的溢出错误不应触发新模型的压缩。

**场景2（压缩后遗留）**：Opus 失败 → 切换到 Codex → 压缩 → 切回 Opus → Opus 的旧错误仍在上下文中，但不应再次触发压缩。

### 11.10 图片的 Token 估算

在 token 估算中，图片内容使用固定估算值：

```typescript
if (block.type === "image") {
  chars += 4800; // 约 1200 tokens
}
```

这是一个保守估计，确保图片密集的对话不会悄悄突破上下文窗口。

---

## 附录：核心源码文件索引

| 文件路径 | 职责 |
|----------|------|
| `packages/agent/src/types.ts` | Agent 消息类型、配置接口定义 |
| `packages/agent/src/agent.ts` | Agent 类：状态管理、消息队列、事件分发 |
| `packages/agent/src/agent-loop.ts` | Agent 循环：LLM 调用、工具执行、steering/follow-up |
| `packages/coding-agent/src/core/messages.ts` | 自定义消息类型、`convertToLlm()` 转换管道 |
| `packages/coding-agent/src/core/session-manager.ts` | 树形会话存储、JSONL 持久化、版本迁移 |
| `packages/coding-agent/src/core/agent-session.ts` | 会话生命周期管理、压缩/重试协调 |
| `packages/coding-agent/src/core/compaction/compaction.ts` | 切割算法、摘要生成、压缩核心逻辑 |
| `packages/coding-agent/src/core/compaction/branch-summarization.ts` | 分支摘要生成 |
| `packages/coding-agent/src/core/compaction/utils.ts` | 文件追踪、消息序列化、通用工具 |
