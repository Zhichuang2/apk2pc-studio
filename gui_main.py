"""图形化版打包入口（--windowed，无黑框）。

用系统 Python 3.14 打包：它自带 tkinter / Tcl-Tk 9，
配合 PyInstaller >= 6.22.3 才能正确收集 tcl 数据目录。
"""
from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# --windowed 打包后 stdout/stderr 是 None；给个无副作用的落点，
# 避免任何第三方库里残留的 print / traceback.print_exc 直接抛 AttributeError。
for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        setattr(sys, _name, io.StringIO())


def _fatal(title: str, detail: str) -> None:
    """--windowed 下没有控制台，出错必须弹窗，绝不能静默闪退。"""
    try:
        import tkinter.messagebox as mb
        root = None
        mb.showerror(title, detail[-1800:])
    except Exception:
        try:
            (Path.home() / "apk2pc-gui-error.txt").write_text(
                title + "\n" + detail, encoding="utf-8")
        except Exception:
            pass


def main() -> int:
    try:
        from gui_app import run_gui
        return run_gui()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        _fatal("图形化版启动失败", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
