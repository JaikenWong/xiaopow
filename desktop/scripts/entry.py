"""PyInstaller 打包入口 — 供 sidecar 单文件可执行使用。

运行 `python3 desktop/scripts/build_sidecar.py` 生成产物。
"""

from xiaopaw.main import main

if __name__ == "__main__":
    main()
