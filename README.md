## XiaoPaw（小爪子）

基于飞书的本地工作助手，通过 Skills 生态 + AIO-Sandbox（Docker）实现安全可扩展的工具调用。支持飞书 WebSocket 长连接，无需公网 IP，适合本地/内网部署。

### 核心功能

- **飞书全场景接入**：单聊（p2p）、群聊（group）、话题群（thread）
- **Skills 生态**：9 个内置 Skill，覆盖文件处理、网页搜索/浏览、飞书操作、定时任务、历史查询
- **AIO-Sandbox 隔离**：所有代码执行在 Docker 沙盒中运行，凭证不经过 LLM
- **Verbose 详细模式**：实时推送 Agent 推理过程，可随时开关
- **定时任务**：支持一次性（at）、固定间隔（every）、Cron 表达式三种模式
- **TestAPI**：HTTP 接口本地调试，无需真实飞书环境
- **卡片消息 + Loading 效果**：发送交互式卡片，Loading 状态实时更新（2026-03-09 新增）
- **Markdown 富文本渲染**：支持 lark_md 格式，Agent 回复支持加粗、斜体、链接等（2026-03-09 新增）

### 内置 Skills

| Skill | 类型 | 能力 |
|-------|------|------|
| `pdf` | 任务型 | PDF 解析、文本提取、格式转换 |
| `docx` | 任务型 | Word 文档读取与处理 |
| `pptx` | 任务型 | PPT 文档读取与处理 |
| `xlsx` | 任务型 | Excel 表格读取与处理 |
| `feishu_ops` | 任务型 | 通过 `scripts/*.py` 脚本读取飞书云文档、向指定群/用户发消息 |
| `scheduler_mgr` | 任务型 | 通过 `scheduler_mgr/scripts/*.py` 创建/查看/更新/删除定时任务 |
| `baidu_search` | 任务型 | 百度千帆网络搜索，支持时间过滤与站点限定 |
| `web_browse` | 任务型 | 网页内容提取（Markdown 转换）与浏览器自动化（截图/表单/JS） |
| `history_reader` | 参考型 | 分页读取历史对话记录 |

### 目录结构

```
xiaopaw/
├── main.py                  # 进程入口
├── models.py                # InboundMessage / Attachment / SenderProtocol
├── runner.py                # 执行引擎（per-routing_key 队列、Slash 命令、Agent 调用）
├── llm/aliyun_llm.py        # AliyunLLM 适配器（通义千问，支持多模态+Function Calling）
├── feishu/
│   ├── listener.py          # WebSocket 事件 → InboundMessage
│   ├── sender.py            # 消息发送（p2p/group/thread），含重试
│   ├── downloader.py        # 附件下载到 session workspace
│   └── session_key.py       # routing_key 解析
├── agents/
│   ├── main_crew.py         # 主 Crew（build_agent_fn 工厂）
│   └── skill_crew.py        # Sub-Crew 工厂（build_skill_crew）
├── tools/
│   ├── skill_loader.py      # SkillLoaderTool（渐进式披露 + Sub-Crew 触发）
│   ├── add_image_tool_local.py
│   ├── baidu_search_tool.py
│   └── intermediate_tool.py
├── session/                 # SessionManager（index.json + JSONL）
├── cron/                    # CronService（asyncio 精确 timer）
├── cleanup/                 # CleanupService（按策略清理过期文件）
├── observability/           # 日志 + Prometheus Metrics
├── api/                     # TestAPI（aiohttp HTTP 服务）
└── skills/                  # SKILL.md + 执行脚本，每个 Skill 独立目录
    ├── pdf/ docx/ pptx/ xlsx/
    ├── feishu_ops/
    ├── scheduler_mgr/
    ├── baidu_search/
    ├── web_browse/
    └── history_reader/
```

### 环境准备

**依赖**：Python 3.11+、Docker（运行 AIO-Sandbox）

```bash
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

**环境变量**：

```bash
export QWEN_API_KEY=<阿里云千问 API Key>
export BAIDU_API_KEY=<百度千帆 API Key>       # baidu_search Skill 需要
# 调试时可选开启完整请求 payload 日志
export QWEN_DEBUG_PAYLOAD=1
```

### 配置 `config.yaml`

复制模板并填写飞书凭证：

```bash
cp config.yaml.template config.yaml
```

核心配置项：

```yaml
feishu:
  app_id: "${FEISHU_APP_ID}"       # 飞书开放平台应用 App ID
  app_secret: "${FEISHU_APP_SECRET}" # 飞书开放平台应用 App Secret

baidu:
  api_key: "${BAIDU_API_KEY}"       # 百度千帆 API Key（baidu_search Skill）

sandbox:
  url: "http://localhost:8022/mcp"  # AIO-Sandbox MCP 地址

debug:
  enable_test_api: true             # 本地调试时开启
  test_api_port: 9090
```

完整配置项见 `config.yaml.template`。

### 启动 AIO-Sandbox

```bash
docker compose -f sandbox-docker-compose.yaml up -d
```

Sandbox MCP 端点：`http://localhost:8022/mcp`

### 启动 XiaoPaw

```bash
python3 -m xiaopaw.main
```

启动后：
- 飞书 WebSocket 开始监听消息
- Prometheus 指标：`http://127.0.0.1:9100/metrics`
- JSON 行日志：`data/logs/xiaopaw.log`
- TestAPI（如已启用）：`http://127.0.0.1:9090/api/test/message`

### 本地调试（TestAPI）

在 `config.yaml` 中设置 `debug.enable_test_api: true`，无需真实飞书环境：

```bash
# 发送消息，同步获取 Bot 回复
curl -X POST http://127.0.0.1:9090/api/test/message \
  -H "Content-Type: application/json" \
  -d '{"routing_key": "p2p:ou_test001", "content": "你好"}'

# 响应示例（Bot 回复已通过卡片消息 + update_card 完整更新）
{
  "msg_id": "test_xxx",
  "reply": "**你好！** 我是 XiaoPaw 工作助手。有什么可以帮助你的吗？",
  "session_id": "s-uuid-001",
  "duration_ms": 2345,
  "skills_called": []
}

# 清空会话数据
curl -X DELETE http://127.0.0.1:9090/api/test/sessions
```

**卡片消息流程**（从 2026-03-09 开始）：
1. 用户发送消息 → Runner 接收
2. Runner 调用 `send_thinking()` → 发送"⏳ 思考中..."加载卡片，获取 card_msg_id
3. Agent 执行（5-30s）
4. Runner 调用 `update_card(card_msg_id, 最终结果)` → 更新卡片内容为 Agent 回复
5. 若更新失败，降级调用 `send()` 重新发送整条消息

### Slash 命令

| 命令 | 功能 |
|------|------|
| `/new` | 创建新会话，之前历史不带入 |
| `/verbose on/off` | 开启/关闭推理过程实时推送 |
| `/verbose` | 查询详细模式当前状态 |
| `/status` | 查看当前会话信息 |
| `/help` | 显示命令帮助 |

### 运行测试

```bash
# 单元测试（含覆盖率）
python3 -m pytest tests/unit/ -v --cov=xiaopaw --cov-report=term-missing

# 集成测试（无 LLM，无 Sandbox）
python3 -m pytest tests/integration/ -m "not llm and not sandbox" -v

# 集成测试（含 LLM，需设置 QWEN_API_KEY）
python3 -m pytest tests/integration/test_e2e_conversation.py -m "llm and not sandbox" -v -s

# 完整集成测试（需启动 Sandbox）
python3 -m pytest tests/integration/ -v -s --timeout=180
```

**测试统计**（2026-03-10）：562 单元测试，86% 覆盖率 ✅

更多设计细节见 `DESIGN.md` 和 `CLAUDE.md`。

---

## 课堂代码演示学习指南

### 整体架构一览

```
飞书消息
   │  WebSocket
   ▼
FeishuListener
   │  解析 → InboundMessage
   ▼
Runner（per-routing_key 串行队列）
   │
   ├─ 斜杠命令？ /new /verbose /help
   │     → 直接处理
   │
   ├─ 有附件？
   │     → FeishuDownloader 下载到 session workspace
   │
   ├─ SessionManager 加载/创建 Session
   │
   ├─ FeishuSender.send_thinking()  ← "正在思考" 卡片
   │
   ▼
Main Crew（单 Agent + SkillLoaderTool）
   │
   │  Phase 1: 看"菜单"
   │  <available_skills> XML 描述
   │
   │  Phase 2: 调用 Skill
   │  skill_loader(skill_name="pdf", task_context={...})
   │
   ▼
Sub-Crew（独立实例 + MCP Sandbox）
   │
   │  sandbox_execute_code / sandbox_execute_bash
   │  sandbox_file_operations
   │
   ▼
FeishuSender.update_card()  ← 更新回复卡片
   │
   ▼
SessionManager.append()  ← 持久化对话历史
```

### 学习路线

---

#### 第一步：看消息接收链路

**阅读文件**：`xiaopaw/feishu/listener.py`

| 事件类型 | 处理 |
|---------|------|
| `im.message.receive_v1` | 解析消息 → InboundMessage → Runner |
| `im.chat.member.bot.added_v1` | 机器人入群通知 |

**理解要点**：飞书使用 WebSocket 长连接（不需要公网 IP），通过 `allowed_chats` 白名单控制哪些群聊可以触发 Agent。

---

#### 第二步：看 Runner 并发模型

**阅读文件**：`xiaopaw/runner.py`

```
routing_key_1（用户A）: ──msg1──msg2──msg3──  串行
routing_key_2（群B）:   ──msg1──msg2──        串行
                        ↕ 并行 ↕
```

| 设计决策 | 原因 |
|---------|------|
| 同一 routing_key 串行 | 同一对话的消息必须按顺序处理 |
| 不同 routing_key 并行 | 不同用户/群互不阻塞 |
| Worker 空闲超时退出 | 释放内存，按需创建 |

**理解要点**：每个 routing_key 有独立的 `asyncio.Queue` + worker 协程。这是"per-key 串行队列"模式——在并发和一致性之间取得平衡。

---

#### 第三步：看双层 Crew 架构

**阅读文件**：`xiaopaw/agents/main_crew.py` + `skill_crew.py`

| 层 | 角色 | LLM | 工具 |
|----|------|-----|------|
| Main Crew | 意图理解 + 任务规划 | qwen3.6-max-preview | SkillLoaderTool |
| Sub-Crew | 具体任务执行 | qwen3-max | MCP Sandbox 工具 |

**理解要点**：
- Main Crew 只有一个 Tool（SkillLoaderTool）——"单工具原则"，所有能力通过 Skill 提供
- Sub-Crew 每次调用都创建新实例（工厂模式），防止状态污染
- Main Crew 的历史不进入 Sub-Crew，Sub-Crew 的执行细节不进入 Main Crew——上下文隔离

---

#### 第四步：看 SkillLoaderTool 渐进式披露

**阅读文件**：`xiaopaw/tools/skill_loader.py`

| 阶段 | 时机 | 加载内容 |
|------|------|---------|
| Phase 1 | 工具初始化 | YAML frontmatter → XML "菜单"（十几个字） |
| Phase 2 | 被调用时 | 完整 SKILL.md 指令（可能几百行） |

**理解要点**：Main Agent 的 context 是稀缺资源。Phase 1 只注入轻量"菜单"，Phase 2 按需加载且缓存。`{var}` 会被转义为 `{{var}}`，防止 CrewAI 模板引擎误解析。

---

#### 第五步：看 9 个内置 Skill

**阅读文件**：`xiaopaw/skills/load_skills.yaml` + 各 Skill 目录

| Skill | 类型 | 能力 |
|-------|------|------|
| `pdf` | task | PDF → Markdown |
| `docx` | task | 生成 Word 文档 |
| `pptx` | task | 生成 PPT |
| `xlsx` | task | 生成 Excel |
| `feishu_ops` | task | 飞书 API（16 个脚本：发消息/读文档/管日历/写表格...） |
| `scheduler_mgr` | task | 定时任务 CRUD |
| `baidu_search` | task | 百度搜索 + 内容抓取 |
| `web_browse` | task | 无头浏览器操控 |
| `history_reader` | reference | 分页读取对话历史（不需要 Sub-Crew） |

---

#### 第六步：看凭证隔离

**阅读文件**：`xiaopaw/cleanup/service.py`（搜索 `_write_credentials`）

```
启动时：CleanupService 写入凭证
  → data/workspace/.config/feishu.json
  → data/workspace/.config/baidu.json
  权限 0600，原子写入（write-then-rename）

Skill 脚本直接读取 .config/ 文件
  → LLM 上下文中没有任何凭证
```

**理解要点**：凭证不通过 Agent 的 backstory 或 Task 描述传递——LLM 永远看不到 API Key。脚本在 Sandbox 内直接读取 `.config/` 文件。

---

#### 第七步：看 Session 管理

**阅读文件**：`xiaopaw/session/manager.py`

| 文件 | 格式 | 作用 |
|------|------|------|
| `index.json` | JSON | routing_key → active_session_id 映射 |
| `{session_id}.jsonl` | JSONL | 对话历史（meta 行 + user/assistant 对） |

**理解要点**：并发安全通过 `asyncio.Lock` + 原子写入（`write-then-rename`）保证。`/new` 斜杠命令创建新 Session，历史清零。

---

#### 第八步：看定时任务

**阅读文件**：`xiaopaw/cron/service.py`

| 模式 | 示例 | 场景 |
|------|------|------|
| `at` | `"2026-04-25T09:00"` | 一次性提醒 |
| `every` | `"30m"` | 固定间隔 |
| `cron` | `"0 9 * * 1-5"` | 标准 cron 表达式 |

**理解要点**：定时任务通过构造 fake `InboundMessage` 走正常 Runner 管线——不需要单独的执行路径。`tasks.json` 支持热加载（mtime + size 双检测）。

---

#### 第九步：看测试和本地调试

**阅读文件**：`xiaopaw/api/test_server.py`

```bash
# 本地测试（不需要飞书环境）
curl -X POST http://localhost:8080/api/test/message \
  -H "Content-Type: application/json" \
  -d '{"routing_key": "test", "content": "帮我搜索 Qwen3 最新动态"}'
```

**理解要点**：`TestAPI` 通过 `CaptureSender`（实现 `SenderProtocol`）拦截 Agent 回复，将异步飞书消息流转化为同步 HTTP 响应——方便本地开发调试。

---

### 学习检查清单

- [ ] per-routing_key 串行队列解决了什么问题？（同一对话按序处理，不同对话并行——兼顾一致性和并发）
- [ ] Main Crew 为什么只有一个 Tool？（单工具原则——所有能力通过 Skill 提供，降低 Agent 决策复杂度）
- [ ] Sub-Crew 为什么每次创建新实例？（工厂模式防状态污染——CrewAI 内部有运行时状态）
- [ ] 凭证为什么不写在 backstory 中？（LLM 能看到 backstory，凭证写在 Sandbox 文件中更安全）
- [ ] reference 和 task 两种 Skill 的区别？（reference 返回文本给 Main Agent，task 启动独立 Sub-Crew 在 Sandbox 执行）
- [ ] 定时任务如何触发 Agent？（构造 fake InboundMessage 走正常 Runner 管线）
- [ ] `CaptureSender` 的作用？（实现 SenderProtocol，将异步消息流转为同步 HTTP 响应，用于本地测试）
