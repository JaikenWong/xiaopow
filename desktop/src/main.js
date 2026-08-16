// XiaoPaw 配置窗口前端逻辑
// 通过 HTTP 与 Python sidecar 的 ConfigServer 通信（http://127.0.0.1:9191）

const API_BASE = "http://127.0.0.1:9191";

// ── 工具 ───────────────────────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`HTTP ${res.status}: ${body}`);
  }
  return res.json();
}

function setConnState(ok) {
  const dot = document.getElementById("conn-dot");
  const txt = document.getElementById("conn-text");
  dot.className = "dot " + (ok ? "ok" : "err");
  txt.textContent = ok ? "已连接" : "未连接";
}

function setTestResult(text, cls = "") {
  const el = document.getElementById("test-result");
  el.textContent = text;
  el.className = "test-result " + cls;
}

function setFeishuBadge(connected) {
  const dot = document.getElementById("st-feishu-dot");
  const txt = document.getElementById("st-feishu-text");
  if (connected) {
    dot.className = "dot ok";
    txt.textContent = "已连接";
  } else {
    dot.className = "dot err";
    txt.textContent = "未连接";
  }
}

// ── Tab 切换 ───────────────────────────────────────────

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`panel-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "status") loadStatus();
    if (btn.dataset.tab === "logs") loadLogs();
  });
});

// ── 状态页 ─────────────────────────────────────────────

async function loadStatus() {
  try {
    const s = await api("/api/status");
    document.getElementById("st-running").textContent = s.running ? "运行中" : "已停止";
    document.getElementById("st-running").style.color = s.running ? "var(--green)" : "var(--yellow)";
    document.getElementById("st-reloads").textContent = s.reloads;
    document.getElementById("st-path").textContent = s.config_path;
    setFeishuBadge(!!s.feishu_connected);

    const errCard = document.getElementById("st-error");
    if (s.error) {
      errCard.classList.remove("hidden");
      document.getElementById("st-error-text").textContent = s.error;
    } else {
      errCard.classList.add("hidden");
    }
    setConnState(true);
  } catch (e) {
    setConnState(false);
    document.getElementById("st-running").textContent = "无法连接";
    document.getElementById("st-running").style.color = "var(--red)";
    setFeishuBadge(false);
  }
}

document.getElementById("btn-refresh-status").addEventListener("click", loadStatus);

// ── 连通测试 ───────────────────────────────────────────

async function testFeishu() {
  const btn = document.getElementById("btn-test-feishu");
  btn.disabled = true;
  setTestResult("测试中…");
  try {
    const r = await api("/api/test-feishu", { method: "POST" });
    if (r.ok) {
      setTestResult(
        `✓ 飞书凭证有效（code=${r.code}, expire=${r.expire}s, ${r.latency_ms}ms）`,
        "ok"
      );
    } else {
      setTestResult(`✗ ${r.error || "未知错误"}`, "err");
    }
  } catch (e) {
    setTestResult(`✗ ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function testLLM() {
  const btn = document.getElementById("btn-test-llm");
  btn.disabled = true;
  setTestResult("测试中…");
  try {
    const r = await api("/api/test-llm", { method: "POST" });
    if (r.ok) {
      setTestResult(`✓ 模型连通（${r.model}, ${r.latency_ms}ms）`, "ok");
    } else {
      setTestResult(`✗ ${r.error || "未知错误"}`, "err");
    }
  } catch (e) {
    setTestResult(`✗ ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("btn-test-feishu").addEventListener("click", testFeishu);
document.getElementById("btn-test-llm").addEventListener("click", testLLM);

// ── 机器人控制 ─────────────────────────────────────────

async function disconnectFeishu() {
  if (!confirm("确认断开飞书机器人？bot 将停止接收消息，cron / 指标 / 清理继续运行。")) return;
  const btn = document.getElementById("btn-disconnect-feishu");
  btn.disabled = true;
  try {
    const r = await api("/api/disconnect-feishu", { method: "POST" });
    if (r.ok) {
      setTestResult("✓ 机器人已断开", "ok");
      setFeishuBadge(false);
    } else {
      setTestResult(`✗ ${r.error || "未知错误"}`, "err");
    }
  } catch (e) {
    setTestResult(`✗ ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function reconnectFeishu() {
  const btn = document.getElementById("btn-reconnect-feishu");
  btn.disabled = true;
  setTestResult("正在重新连接…");
  try {
    const r = await api("/api/reconnect-feishu", { method: "POST" });
    if (r.ok) {
      setTestResult("✓ 已触发重连，等待服务重建…", "ok");
      // 重建需要几秒，延迟刷新
      setTimeout(loadStatus, 2000);
    } else {
      setTestResult(`✗ ${r.error || "未知错误"}`, "err");
    }
  } catch (e) {
    setTestResult(`✗ ${e.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("btn-disconnect-feishu").addEventListener("click", disconnectFeishu);
document.getElementById("btn-reconnect-feishu").addEventListener("click", reconnectFeishu);

// ── 配置页 ─────────────────────────────────────────────

function getByPath(obj, path) {
  return path.split(".").reduce((acc, k) => (acc == null ? undefined : acc[k]), obj);
}

function setByPath(obj, path, value) {
  const keys = path.split(".");
  let cur = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (cur[keys[i]] == null || typeof cur[keys[i]] !== "object") cur[keys[i]] = {};
    cur = cur[keys[i]];
  }
  cur[keys[keys.length - 1]] = value;
}

async function loadConfig() {
  try {
    const { config } = await api("/api/config");
    const form = document.getElementById("config-form");
    form.querySelectorAll("input[name]").forEach((input) => {
      const val = getByPath(config, input.name);
      if (Array.isArray(val)) {
        input.value = val.join(",");
      } else if (val != null) {
        input.value = val;
      }
    });
    setConnState(true);
  } catch (e) {
    setConnState(false);
    document.getElementById("save-status").textContent = "加载配置失败: " + e.message;
    document.getElementById("save-status").className = "save-status err";
  }
}

document.getElementById("btn-save").addEventListener("click", async () => {
  const status = document.getElementById("save-status");
  status.className = "save-status";
  status.textContent = "保存中…";

  const form = document.getElementById("config-form");
  const config = {};

  form.querySelectorAll("input[name]").forEach((input) => {
    let value = input.value;
    if (input.type === "number") {
      value = value === "" ? "" : Number(value);
    }
    if (input.name === "feishu.allowed_chats") {
      value = value.split(",").map((s) => s.trim()).filter(Boolean);
    }
    setByPath(config, input.name, value);
  });

  try {
    const resp = await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config }),
    });
    status.className = "save-status ok";
    status.textContent = "已保存，服务正在重启…";
    setTimeout(loadStatus, 1500);
  } catch (e) {
    status.className = "save-status err";
    status.textContent = "保存失败: " + e.message;
  }
});

// ── 日志页 ─────────────────────────────────────────────

async function loadLogs() {
  const view = document.getElementById("log-view");
  try {
    const { lines } = await api("/api/logs?lines=200");
    view.textContent = lines.length ? lines.join("\n") : "（暂无日志）";
    setConnState(true);
  } catch (e) {
    view.textContent = "加载日志失败: " + e.message;
    setConnState(false);
  }
}

document.getElementById("btn-refresh-logs").addEventListener("click", loadLogs);

// ── 初始化 ─────────────────────────────────────────────

loadStatus();
loadConfig();
// 每 10 秒刷新一次连接状态
setInterval(() => {
  if (document.querySelector(".nav-item.active").dataset.tab === "status") loadStatus();
}, 10000);
