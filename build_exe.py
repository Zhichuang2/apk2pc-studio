"""打包：python build_exe.py [--onedir]

默认 onefile：产物是**单个 exe**，拷到没装 Python 的电脑上双击即可，
不会因漏带 _internal 目录而闪退（实测冷启动 0.69 s，与 onedir 持平）。
--onedir 仍可用（目录形式），此时会额外产出一个 onefile 的 launcher-base.exe
供「生成独立启动器」使用，保证成品同样是单文件。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "APK2PCStudio"


def _park(path: Path) -> None:
    """不删旧产物，改名让位（某些环境把删除劫持成移入回收站会锁目录）。"""
    if not path.exists():
        return
    for i in range(1, 100):
        target = path.with_name(f"{path.name}.old{i}")
        if not target.exists():
            try:
                path.rename(target)
                return
            except OSError:
                return


def _pyinstaller(onefile: bool, name: str, distpath: Path, workpath: Path) -> int:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--console",
        "--name", name,
        "--add-data", f"{ROOT / 'web'};web",
        # apktool.jar 不内置：首次运行从 Maven 中央仓取（省 15 MB，启动更快）
        "--collect-submodules", "core",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "--exclude-module", "tkinter",
        "--distpath", str(distpath),
        "--workpath", str(workpath),
        str(ROOT / "app.py"),
    ]
    print(">>> PyInstaller", "--onefile" if onefile else "--onedir", name, flush=True)
    return subprocess.call(args, cwd=str(ROOT))


def main() -> int:
    want_onedir = "--onedir" in sys.argv
    _park(ROOT / "dist")
    _park(ROOT / "build")
    (ROOT / f"{NAME}.spec").unlink(missing_ok=True)

    dist = ROOT / "dist"
    rc = _pyinstaller(not want_onedir, NAME, dist, ROOT / "build")
    if rc != 0:
        return rc

    if want_onedir:
        # onedir 主程序旁边补一个单文件模板，供生成独立启动器用
        rc = _pyinstaller(True, "launcher-base", dist, ROOT / "build_launcher")
        if rc == 0:
            dst = dist / NAME / "launcher-base.exe"
            shutil.copy2(dist / "launcher-base.exe", dst)
            print(f"  launcher-base.exe → {dst}")
        out = dist / NAME / f"{NAME}.exe"
    else:
        out = dist / f"{NAME}.exe"

    if out.exists():
        print(f"\n✔ 打包完成: {out}  ({out.stat().st_size/1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
