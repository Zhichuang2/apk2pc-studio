"""打包：python build_exe.py [--onefile]

默认 onedir：启动 1s 内（onefile 每次要把整个包解到临时目录，实测 2.8s）。
--onefile 仍可用（想要「单文件分发」时），但启动会慢 2 秒左右。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "APK2PCStudio"


def _park(path: Path) -> Path | None:
    """不删旧产物，改名让位。

    某些环境把删除劫持成「移到回收站」，目录稍大就失败并抛 OSError，
    连 PyInstaller 自己的清理都会被打断；改名是唯一稳妥的做法。
    """
    if not path.exists():
        return None
    for i in range(1, 100):
        target = path.with_name(f"{path.name}.old{i}")
        if not target.exists():
            try:
                path.rename(target)
                return target
            except Exception:
                return None
    return None


def main() -> int:
    onefile = "--onefile" in sys.argv
    _park(ROOT / "dist")
    _park(ROOT / "build")
    _park(ROOT / "build_v2")
    spec = ROOT / f"{NAME}.spec"
    if spec.exists():
        spec.unlink(missing_ok=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--console",
        "--name", NAME,
        "--add-data", f"{ROOT / 'web'};web",
        # vendor/apktool.jar 不再内置：改为首次运行从 Maven 中央仓取（省 15 MB，启动更快）
        "--collect-submodules", "core",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "--exclude-module", "tkinter",
        str(ROOT / "app.py"),
    ]
    print(">>>", " ".join(args))
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc == 0:
        out = ROOT / "dist" / (f"{NAME}.exe" if onefile else NAME)
        print(f"\n✔ 打包完成: {out}")
        if out.exists():
            if out.is_file():
                print(f"  体积: {out.stat().st_size/1048576:.1f} MB")
            else:
                total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
                print(f"  体积: {total/1048576:.1f} MB（目录）")
                print(f"  可执行: {out / (NAME + '.exe')}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
