"""发布构建：一次产出 v1.2.0 的全部资产。

    python build_release.py

产物（在项目上级目录的 _release/）：
    APK2PCStudio-1.2.0-CLI.exe         命令行向导版
    APK2PCStudio-1.2.0-Web.exe         浏览器界面版
    APK2PCStudio-1.2.0-GUI.exe         图形窗口版（tkinter）
    APK2PCStudio-1.2.0-AllInOne.zip    三合一（3 个 exe + 界面选择器 + 说明）
    APK2PCStudio-1.2.0-Source.zip      源码
    APK2PCStudio-1.2.0-Source.tar.gz   源码

 CLI / Web / 选择器是**同一个二进制**按文件名分派，只打一次包；
 GUI 需要 tkinter，用系统 Python 3.14 单独打（PyInstaller >= 6.22.3）。
源码包用 git archive，天然不含 dist/build/密钥。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT.parent / "_release"
WORK = ROOT.parent / "_build_release"
VER = "1.2.0"
BASE = f"APK2PCStudio-{VER}"

PY314 = Path(r"C:\Users\ZC\AppData\Local\Programs\Python\Python314\python.exe")


def _find_py314() -> Path:
    """系统 Python 3.14（带 tkinter）；找不到就明确报错，不静默降级。"""
    if PY314.exists():
        return PY314
    for p in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python314" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python313" / "python.exe",
    ):
        if p.exists():
            return p
    raise RuntimeError("未找到带 tkinter 的系统 Python（需要 3.13/3.14 完整安装版）")


def _pyinstaller(python: Path, script: Path, name: str,
                 dist: Path, work: Path, *, windowed: bool = False) -> Path:
    args = [
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--windowed" if windowed else "--console",
        "--name", name,
        "--add-data", f"{ROOT / 'web'};web",
        "--collect-submodules", "core",
        "--exclude-module", "numpy", "--exclude-module", "PIL",
    ]
    if windowed:
        # GUI 版必须保留 tkinter；gui_app 被 gui_main 静态 import，显式兜底
        args += ["--hidden-import", "gui_app"]
    else:
        # CLI/Web/选择器不付 tkinter 与 GUI 的体积
        args += ["--exclude-module", "tkinter", "--exclude-module", "gui_app"]
    args += ["--distpath", str(dist), "--workpath", str(work), str(script)]
    print(f"\n>>> 打包 {name}  [{python.name}, {'windowed' if windowed else 'console'}]", flush=True)
    rc = subprocess.call(args, cwd=str(ROOT))
    if rc != 0:
        raise RuntimeError(f"PyInstaller 失败：{name} (rc={rc})")
    exe = dist / f"{name}.exe"
    if not exe.exists():
        raise RuntimeError(f"未产出 {exe}")
    print(f"    ✔ {exe}  ({exe.stat().st_size/1048576:.1f} MB)", flush=True)
    return exe


def _write_note(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # ---- 1. CLI / Web / 选择器共用的主二进制（当前 3.13，已验证稳定）
    base_exe = _pyinstaller(Path(sys.executable), ROOT / "app.py",
                            "APK2PCStudio", WORK / "dist_cli", WORK / "build_cli")

    cli = OUT / f"{BASE}-CLI.exe"
    web = OUT / f"{BASE}-Web.exe"
    selector = OUT / "APK2PCStudio.exe"
    for dst in (cli, web, selector):
        shutil.copy2(base_exe, dst)

    # ---- 2. GUI 版（Python 3.14 + tkinter）
    gui_exe = _pyinstaller(_find_py314(), ROOT / "gui_main.py",
                           "APK2PCStudio-GUI", WORK / "dist_gui", WORK / "build_gui",
                           windowed=True)
    gui = OUT / f"{BASE}-GUI.exe"
    shutil.copy2(gui_exe, gui)

    # ---- 3. 三合一包：三个专用版 + 一个界面选择器 + 说明
    note = f"""APK2PC Studio {VER} · 三合一版
========================================

这个压缩包里有三种界面，任选一个双击即可，**都不需要安装 Python**。

  APK2PCStudio.exe              界面选择器：双击后自己选用哪种界面
  APK2PCStudio-{VER}-CLI.exe    命令行向导：黑框菜单，最省资源
  APK2PCStudio-{VER}-Web.exe    浏览器界面：自动开网页，功能最全
  APK2PCStudio-{VER}-GUI.exe    图形窗口：像普通软件一样能点按钮

新手建议：直接双击「APK2PCStudio.exe」，按 1 回车，然后把 APK 拖进去。

----------------------------------------
第一次使用
----------------------------------------
1. 需要 Windows 10/11 x64、JDK 11+、BIOS 开启虚拟化并启用
   「Windows 虚拟机监控程序平台」(WHPX)。
2. 首次运行会下载工具链与安卓运行时（约 2 GB，只下一次）。
   找不到模拟器时会询问你安装在哪，确认后登记到 PATH，
   以后所有版本、所有成品共享，不会重复下载。
3. 选一个 APK → 一键全流程：解包 → Java 源码 → 重建签名 → 装机 → 打开。

----------------------------------------
产物在哪
----------------------------------------
成品默认放在程序目录下的 output\\，可在界面/菜单里切换。
解包出的 src\\ 可以改 smali 和资源，改完用「重新打包签名」出新包。
签名后只留一份可安装的成品，中间副本自动删除。

「生成单文件启动器」会把重建签名后的 APK 焊进一个 exe 尾部，
拷到任何 Windows 电脑双击即用（运行时仍然全局共享）。

想用某个特定界面而不想每次选：直接双击对应的专用 exe。
也可以设环境变量 APK2PC_MODE=cli|web|gui|select 强制指定。

----------------------------------------
合规
----------------------------------------
仅用于你自己拥有合法授权的应用。本工具不提供破解、脱壳或绕过授权。
"""
    allinone = OUT / f"{BASE}-AllInOne.zip"
    with zipfile.ZipFile(allinone, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(cli, f"APK2PCStudio/{cli.name}")
        z.write(web, f"APK2PCStudio/{web.name}")
        z.write(gui, f"APK2PCStudio/{gui.name}")
        z.write(selector, "APK2PCStudio/APK2PCStudio.exe")
        z.writestr("APK2PCStudio/使用说明.txt", note)
    print(f"\n    ✔ 三合一 {allinone.name}  ({allinone.stat().st_size/1048576:.1f} MB)", flush=True)

    # ---- 4. 两份源码（git archive，自动排除 dist/build/密钥/APK）
    def archive(fmt: str, suffix: str) -> Path:
        dest = OUT / f"{BASE}-Source.{suffix}"
        with open(dest, "wb") as f:
            rc = subprocess.call(["git", "archive", "--format=" + fmt,
                                 "--prefix=apk2pc-studio/", "HEAD"],
                                cwd=str(ROOT), stdout=f)
        if rc != 0:
            raise RuntimeError(f"git archive 失败：{fmt}")
        print(f"    ✔ 源码 {dest.name}  ({dest.stat().st_size/1048576:.2f} MB)", flush=True)
        return dest

    archive("zip", "zip")
    archive("tar.gz", "tar.gz")

    # ---- 5. 汇总
    print("\n" + "=" * 60, flush=True)
    print("资产清单：", flush=True)
    for p in sorted(OUT.glob(f"{BASE}*")):
        print(f"  {p.name:42s} {p.stat().st_size/1048576:7.2f} MB", flush=True)
    print("=" * 60, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
