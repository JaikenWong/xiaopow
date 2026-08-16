# AGENTS.md — XiaoPaw (小爪子)

飞书本地工作助手：Python 3.12+ asyncio + CrewAI + AIO-Sandbox (Docker MCP)。通过飞书 WebSocket 长连接收消息，单进程内 CrewAI Agent 调度，所有代码执行隔离在沙盒。完整设计见 `DESIGN.md` 与 `docs/`。

## 快速上手

```bash
pip install -r requirements.txt          # 真实依赖清单（pyproject.toml 不完整）
cp config.yaml.template config.yaml      # config.yaml 已 .gitignore
docker compose -f sandbox-docker-compose.yaml up -d   # 必须：localhost:8022
export MINIMAX_API_KEY=...                # 主 Agent LLM
export BAIDU_API_KEY=...                  # 可选：baidu_search Skill
python3 -m xiaopaw.main
```

端点：
- 飞书 WS（无指标，默认连上即工作）
- Prometheus: `http://127.0.0.1:9100/metrics`
- 日志: `data/logs/xiaopaw.log`（JSON 行）
- TestAPI（仅 `debug.enable_test_api: true`）: `http://127.0.0.1:9090/api/test/message`

## 运行测试

```bash
# 单元测试 + 覆盖率（fail_under=80）
python3 -m pytest tests/unit/ -v --cov=xiaopaw --cov-report=term-missing

# 单文件 / 单测试
python3 -m pytest tests/unit/test_runner.py::TestSlashNew::test_creates_new_session -v

# 集成 — 无 LLM、无 Sandbox（最快）
python3 -m pytest tests/integration/ -m "not llm and not sandbox" -v

# 集成 — 真实 LLM（需 MINIMAX_API_KEY）
python3 -m pytest tests/integration/test_e2e_conversation.py -m "llm and not sandbox" -v -s

# 集成 — 沙盒调用（需 AIO-Sandbox 运行中）
python3 -m pytest tests/integration/ -m "sandbox" -v -s
```

Markers（`tests/integration/conftest.py`）：
- `llm` — 自动 skip 缺 `MINIMAX_API_KEY`
- `sandbox` — 自动 skip `localhost:8022` 不可达
- `feishu` — 需真实飞书凭证
- `integration`

## 关键架构约束（agent 容易踩坑）

- **Main Agent 只有 1 个 tool**：`SkillLoaderTool` (`xiaopaw/tools/skill_loader.py:1`)。所有能力通过 Skills 间接调用。新增能力不要给 Main Agent 加 tool。
- **session_id 永不入 LLM**：作为 `SkillLoaderTool` 的 `PrivateAttr` 持有，模板里只暴露路径字符串。改 `agents/config/tasks.yaml` 或 `akickoff(inputs={})` 时不要泄 `session_id`。
- **凭证隔离**：飞书 / 百度凭证由 `CleanupService.write_feishu_credentials()` / `write_baidu_credentials()` 写入沙盒 `data/workspace/.config/`，**绝不经过 LLM**。新增需要凭证的 Skill 时，把凭证写到 `.config/`，不要塞 prompt。
- **Sub-Crew 每次新建**：`build_skill_crew()` 工厂模式，禁用状态缓存。
- **Sandbox MCP 工具无白名单**：约束靠 Agent backstory 行为规则，而非 `create_static_tool_filter`。`web_browse` 依赖 `browser_*` 工具，**不要**把过滤器加回去。
- **SKILL.md 模板变量安全**：`_get_skill_instructions()` 自动转义 `{var}` → `{{var}}` 防 CrewAI "Template variable not found"。改 SKILL.md 时保留此行为。
- **per-routing_key 队列**：同 session 串行（`asyncio.Queue`），跨 session 并行；worker idle 超时自动退出。配置：`runner.queue_idle_timeout_s`、`runner.max_queue_size`。
- **CrewAI tracing 必须关闭**：`main.py:_disable_crewai_tracing_prompts()` 设 `CREWAI_TRACING_ENABLED=false` + `set_suppress_tracing_messages(True)`。守护进程场景下未关闭会卡 20s 在 `input()` 上。
- **进程单实例**：无跨进程文件锁；并发靠 `asyncio.Lock` + `write-then-rename` 原子写 + JSONL `flush+fsync`。

## 路由键

3 种 `routing_key` 格式（`xiaopaw/feishu/session_key.py`）：
- `p2p:{open_id}` — 单聊（始终放行）
- `group:{chat_id}` — 群聊（受 `feishu.allowed_chats` 白名单约束）
- `thread:{chat_id}:{thread_id}` — 话题群

## Skills 系统

- 注册表：`xiaopaw/skills/load_skills.yaml`（`enabled: false` 跳过）
- 定义：`xiaopaw/skills/{name}/SKILL.md`，YAML frontmatter（name/description/type/version）
- 三种 type：
  - `task` — 触发 Sub-Crew 在沙盒跑
  - `reference` — 直接把 SKILL.md 内容喂给 Main Agent 自推理
  - `host` — 宿主机内联处理（如 `claude_code` 调用本机 Claude Code CLI）
- `feishu_ops` 用独立脚本 `scripts/*.py` + 共享 `_feishu_auth.py`，每个脚本输出 JSON 到 stdout、exit 0。新增脚本保持此约定便于 `tests/unit/test_feishu_ops_scripts.py` 自动覆盖。

## 配置约定

- 所有超时/上限在 `config.yaml` 可调：`agent.max_iter`、`sandbox.timeout_s`、`runner.queue_idle_timeout_s`、`sender.max_retries` + `retry_backoff: [1, 2, 4]`
- `data_dir` 相对路径相对 config.yaml 所在目录解析（不是 cwd）
- LLM：`agent.model` / `agent.base_url` / `agent.api_key`，留空走 `MINIMAX_API_KEY` + 默认 MiniMax M3 endpoint
- 调试：`MINIMAX_DEBUG_PAYLOAD=1` 输出完整 LLM 请求 payload

## Slash 命令（Runner 拦截，Agent 看不到）

`/new` · `/verbose on/off` · `/verbose` · `/status` · `/help` — 在 `xiaopaw/runner.py` 处理，**不要**让 Agent 处理。

## 桌面模式

```bash
python3 -m xiaopaw.main --config <path> --api-port 9191   # api-port > 0 启用桌面 sidecar
```

- `--api-port > 0` 触发 `xiaopaw/api/config_server.py`（GET/PUT 配置、状态、日志流）
- 桌面模式缺 `config.yaml` 时自动生成默认配置（等待前端 PUT）
- 配置变更走热重启（`reload_evt`）；`SIGINT/SIGTERM` 优雅退出
- 打包：`xiaopaw.spec`（PyInstaller），输出 `dist/xiaopaw`

## 目录速查

```
xiaopaw/main.py             # 入口（启动顺序见 docstring）
xiaopaw/runner.py           # 核心：队列 + slash + send_thinking/update_card
xiaopaw/agents/main_crew.py # build_agent_fn 工厂
xiaopaw/agents/skill_crew.py# Sub-Crew 工厂
xiaopaw/tools/skill_loader.py
xiaopaw/feishu/{listener,sender,downloader,session_key}.py
xiaopaw/cron/service.py     # asyncio 精确 timer + mtime 热加载
xiaopaw/session/manager.py  # index.json + JSONL（atomic 写）
xiaopaw/cleanup/service.py  # sweep + 凭证注入
xiaopaw/observability/      # 日志 + Prometheus
xiaopaw/api/                # TestAPI + CaptureSender + ConfigServer
xiaopaw/skills/             # SKILL.md + scripts
data/                       # .gitignore，会话/trace/cron/workspace
docs/                       # 设计文档（modules/data/api/observability）
```

## 不要做

- 不要给 Main Agent 加新 tool — 走 Skill
- 不要把凭证塞 LLM prompt — 写沙盒 `.config/`
- 不要在 `data/` 下手动改文件再期待持久化（重启会被 sweep 清理）
- 不要把 `session_id` 加到 `tasks.yaml` 模板或 `akickoff(inputs=...)`
- 不要给 Sub-Crew 加 `create_static_tool_filter`（会断 `browser_*`）