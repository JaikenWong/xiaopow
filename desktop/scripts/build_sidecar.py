#!/usr/bin/env python3
"""打包 Python sidecar 为单文件可执行，供 Tauri 作为 externalBin 使用。

用法（在仓库根目录运行，确保 xiaopaw 包可 import）：
    python3 desktop/scripts/build_sidecar.py

产物：
    src-tauri/binaries/xiaopaw-<target-triple>[.exe]

依赖：
    pip install pyinstaller
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BINARIES_DIR = REPO_ROOT / "desktop" / "src-tauri" / "binaries"
ENTRY = Path(__file__).resolve().parent / "entry.py"


def rust_target_triple() -> str:
    """将当前平台映射为 Rust target triple（Tauri sidecar 命名约定）。"""
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(machine, machine)

    system = platform.system().lower()
    if system == "darwin":
        return f"{arch}-apple-darwin"
    if system == "windows":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def main() -> int:
    if not ENTRY.exists():
        print(f"entry.py not found: {ENTRY}")
        return 1

    triple = rust_target_triple()
    is_windows = platform.system().lower() == "windows"
    exe_name = "xiaopaw.exe" if is_windows else "xiaopaw"

    # 数据文件：skills 与 agents/config 是运行时必需资源
    skills_src = REPO_ROOT / "xiaopaw" / "skills"
    agents_cfg_src = REPO_ROOT / "xiaopaw" / "agents" / "config"

    # PyInstaller --add-data 分隔符：Windows 用 ;，其余用 :
    data_sep = ";" if is_windows else ":"

    datas = []
    if skills_src.exists():
        datas.append(f"{skills_src}{data_sep}xiaopaw/skills")
    if agents_cfg_src.exists():
        datas.append(f"{agents_cfg_src}{data_sep}xiaopaw/agents/config")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "xiaopaw",
        # crewai / lark_oapi 有动态导入与数据文件，collect-all 最稳妥
        "--collect-all",
        "crewai",
        "--collect-all",
        "lark_oapi",
        # 常见隐式导入
        "--hidden-import",
        "pydantic",
        "--hidden-import",
        "aiohttp",
        "--hidden-import",
        "yaml",
    ]
    for d in datas:
        cmd += ["--add-data", d]
    cmd.append(str(ENTRY))

    print("Running PyInstaller...")
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print("PyInstaller failed")
        return result.returncode

    # 拷贝产物到 Tauri binaries 目录（带 target triple 后缀）
    dist = REPO_ROOT / "dist" / exe_name
    if not dist.exists():
        print(f"产物未找到：{dist}")
        return 1

    BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if is_windows else ""
    target = BINARIES_DIR / f"xiaopaw-{triple}{suffix}"
    shutil.copy2(dist, target)
    print(f"✅ sidecar 已生成：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
