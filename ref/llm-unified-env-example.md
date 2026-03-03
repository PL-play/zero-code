# llm_unified 环境变量（py-mono 模式）

仅保留两类兼容接口：
- `openai_compatible`
- `anthropic_compatible`

你只需要为每类填三项：`BASE_URL`、`MODEL`、`API_KEY`。

## OpenAI-compatible

```env
OPENAI_COMPAT_BASE_URL=https://api.deepseek.com/v1
OPENAI_COMPAT_MODEL=deepseek-chat
OPENAI_COMPAT_API_KEY=your-openai-compatible-key
# 可选
OPENAI_COMPAT_PROVIDER=deepseek
```

可替换为其它兼容服务：
- Qwen: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- GLM: `https://open.bigmodel.cn/api/paas/v4`
- Kimi: `https://api.moonshot.cn/v1`
- MiniMax: `https://api.minimaxi.com/v1`

## Anthropic-compatible

```env
ANTHROPIC_COMPAT_BASE_URL=https://api.anthropic.com
ANTHROPIC_COMPAT_MODEL=claude-3-7-sonnet-20250219
ANTHROPIC_COMPAT_API_KEY=your-anthropic-key
# 可选
ANTHROPIC_COMPAT_PROVIDER=anthropic
```

## 运行示例

```bash
/Users/zhangran/work/plplay/zero-code/.venv/bin/python examples/llm_unified_quickstart.py \
  --api openai_compatible \
  --base-url "$OPENAI_COMPAT_BASE_URL" \
  --model "$OPENAI_COMPAT_MODEL" \
  --api-key "$OPENAI_COMPAT_API_KEY" \
  --provider "${OPENAI_COMPAT_PROVIDER:-openai-compatible}" \
  --prompt "请只回复 pong"
```
