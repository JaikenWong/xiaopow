# XiaoPaw Desktop

将 XiaoPaw 封装为可安装桌面应用（Windows + macOS）的 Tauri 外壳。

## 架构

```
┌─────────────────────────────────────────────┐
│  Tauri 外壳（Rust）                          │
│  - 窗口 + 配置/状态/日志 UI（HTML/JS/CSS）    │
│  - 启动并守护 Python sidecar 子进程           │
└──────────────┬──────────────────────────────┘
               │ spawn + 生命周期管理
┌──────────────▼──────────────────────────────┐
│  Python sidecar（PyInstaller 打包）           │
│  - xiaopaw.main --config <path> --api-port 9191 │
│  - 飞书监听 + Agent + Claude Code Skill       │
│  - ConfigServer（aiohttp，127.0.0.1:9191）    │
│    GET/PUT /api/config · /api/status · /api/logs │
└─────────────────────────────────────────────┘

前端（HTML/JS）──HTTP──▶ ConfigServer（sidecar 内）
```

前端不直接调用 Rust，而是通过 HTTP 与 sidecar 的 ConfigServer 通信，
因此前端逻辑与 Tauri 解耦，便于调试（可直接用浏览器打开 index.html 调试 UI）。

## 目录结构

```
desktop/
├── package.json          # npm 依赖（@tauri-apps/cli）
├── src/                  # 前端（Tauri 加载的 Web 资源）
│   ├── index.html        # 配置窗口（状态/配置/日志三页）
│   ├── main.js           # 前端逻辑（fetch ConfigServer API）
│   └── styles.css        # UI 样式
├── src-tauri/
│   ├── Cargo.toml        # Rust 依赖
│   ├── tauri.conf.json   # Tauri 配置（externalBin 绑定 sidecar）
│   ├── build.rs
│   ├── icons/            # 应用图标
│   ├── binaries/         # PyInstaller 打包产物（sidecar）
│   └── src/main.rs       # 启动/守护 sidecar
└── scripts/
    └── build_sidecar.py  # PyInstaller 打包脚本
```

## 环境准备

- **Node.js** + npm
- **Rust**（`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`）
- **Python 3.12+** + PyInstaller
- 平台构建工具：macOS 需 Xcode CLT；Windows 需 MSVC Build Tools

## 构建步骤

### 1. 打包 Python sidecar

```bash
cd desktop
python3 scripts/build_sidecar.py
```

产物输出到 `src-tauri/binaries/`，命名带平台三元组后缀（如
`xiaopaw-x86_64-apple-darwin`、`xiaopaw-x86_64-pc-windows-msvc.exe`）。
Tauri 打包时会按 `tauri.conf.json` 的 `externalBin` 自动解析。

> ⚠️ PyInstaller 打包 crewai/lark-oapi 时需收集动态导入的数据文件，
> 详见 `scripts/build_sidecar.py` 中的 hiddenimports 配置。

### 2. 生成应用图标

```bash
# 用一张 1024x1024 PNG 生成全套图标
cd desktop
npm install
npx tauri icon path/to/icon.png
```

### 3. 构建安装包

```bash
cd desktop
npm install
npm run build
```

产物在 `src-tauri/target/release/bundle/`：
- macOS：`.dmg` + `.app`
- Windows：`.msi` + `.exe`（NSIS）

## GitHub Actions 自动发布

推送 `v*` 格式的 tag 即自动构建全平台安装包并上传到 GitHub Release（草稿）：

```bash
git tag v0.1.0
git push origin v0.1.0
```

工作流：`.github/workflows/release.yml`

- **三平台矩阵**：
  - `macos-13`（Intel）→ `x86_64-apple-darwin` .dmg
  - `macos-latest`（Apple Silicon）→ `aarch64-apple-darwin` .dmg
  - `windows-latest` → `x86_64-pc-windows-msvc` .msi/.exe
- **流程**：Setup Rust/Node/Python → pip 装依赖 → PyInstaller 打 sidecar →
  `npm install` → `tauri build` → 上传 Release
- **权限**：workflow 需要 `contents: write`（自动建 Release 上传产物），
  仓库 Settings → Actions → Workflow permissions 需允许

### CI 注意事项

- **未签名**：macOS 产物未签名（无 Developer ID 证书），用户首次打开需
  `右键 → 打开` 或 `xattr -cr /Applications/XiaoPaw.app`；Windows 会有 SmartScreen 提示
- **首次跑较慢**：crewai/lark_oapi 依赖大，PyInstaller 打包 + Tauri 编译
  单平台约 10-15 分钟，三平台并行
- **本地手动触发**：Actions 页面可手动 `Run workflow`

## 开发调试

```bash
# 1. 单独启动 sidecar（终端观察日志）
python3 -m xiaopaw.main --config /tmp/xiaopaw-config.yaml --api-port 9191

# 2. 前端直接浏览器打开调试（绕过 Tauri）
open desktop/src/index.html   # 或直接用 dev 服务器

# 3. 完整 Tauri 开发模式（sidecar 需先放好 binary）
cd desktop && npm run dev
```

## 关键约定

- **端口 9191**：前端与 sidecar 的约定端口，改需同步 `main.js` 的 `API_BASE`
  和 `main.rs` 的 `--api-port` 参数。端口固定，被外部进程占用时 sidecar
  快速失败报错（不再静默换端口，避免前端请求落空）
- **sidecar 复用**：sidecar 启动时写 `config_dir/xiaopaw.pid`；外壳发现 9191
  已监听且 pid 文件有效时复用该实例（不再重复 spawn），退出时按 pid kill 防止僵尸
- **配置路径**：桌面模式用系统应用数据目录（`app_data_dir()`），
  首次运行自动生成默认配置模板，用户通过配置窗口填写
- **密钥安全**：ConfigServer 只监听 127.0.0.1；GET /api/config 脱敏；
  写回时 `******` 占位保留旧值
- **热重启**：PUT /api/config 成功后 sidecar 内部重建服务（listener/cron 等），
  无需重启整个进程
