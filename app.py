"""APK2PC Studio 入口。

三种运行态：
1) 命令行向导（默认）：双击就用，不弹浏览器、不占端口、出错会停住显示原因。
2) 浏览器界面：--web，功能最全（保留给进阶使用）。
3) 单 APK 启动器：exe 尾部内嵌 APK 或旁挂 <名字>.apk2pc.json，
   直接「检查共享运行时 → 装机 → 拉起」，全程命令行输出。

设计参考 APK2EXE Launcher：控制台打印每一步，任何异常都 catch 后暂停，绝不闪退。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config as C          # noqa: E402
from core import apk as apkmod         # noqa: E402
from core import launcher, pipeline, provision, runtime  # noqa: E402


def _setup_console() -> None:
    """Windows 控制台默认 GBK，中文会变乱码；统一切到 UTF-8。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def _pause(msg: str = "按回车键退出…") -> None:
    print()
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def load_sidecar() -> dict | None:
    """单 APK 启动器模式：优先读 exe 尾部内嵌的 APK，其次读同名 .apk2pc.json。"""
    if not C.is_frozen():
        return None
    exe = Path(sys.executable)

    emb = launcher.read_embedded(exe)
    if emb:
        label = apkmod.sanitize(emb.get("label") or exe.stem)
        apk = launcher.extract_apk(exe, emb, C.DATA_DIR / "apps" / label)
        if apk:
            emb["apk"] = str(apk)
            return emb

    side = exe.parent / f"{exe.stem}.apk2pc.json"
    if side.exists():
        try:
            return json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def run_unattended(cfg: dict) -> int:
    """启动器模式：不起 HTTP 服务、不开浏览器，直接跑流水线并打印进度。"""
    from core.cli import log, run_job, say, show_result

    apk = Path(cfg.get("apk", ""))
    if not apk.exists():
        say(f"✘ 找不到 APK：{apk}")
        return 1
    if cfg.get("avd"):
        C.settings["avd"] = cfg["avd"]

    from core.cli import ask

    say("=" * 60)
    say(f"  {apkmod.sanitize(cfg.get('label') or apk.stem)}")
    say("=" * 60)
    say("[1/2] 检查共享运行时（已装过的直接复用，不重复下载）…")
    try:
        # 成品只负责「跑 APK」，不装配 apktool / jadx / build-tools：
        # 少下 ~80 MB、少加载一堆模块，多个成品共享同一份运行时。
        root, need_install = provision.resolve_runtime(ask, log)
        if root is None:
            say("✘ 没能确定模拟器位置，已退出")
            return 1
        if need_install:
            say(f"首次使用，正在把安卓运行时装到 {root}（只装这一次）…")
        provision.ensure_emulator_runtime(log)
        provision.sync_tool_path(log)
    except Exception as e:  # noqa: BLE001
        say(f"✘ 运行时未就绪：{e}")
        return 1

    info = {"package": cfg.get("package", ""), "main_activity": cfg.get("activity", "")}
    if not info["package"]:
        info = apkmod.badging_info(apk) or info
    say("[2/2] 安装并启动…")
    ok, res = run_job("run", {
        "apk": str(apk), "info": info,
        "fullscreen": bool(cfg.get("fullscreen", False)),
    })
    show_result(res)
    return 0 if ok else 1


def run_web(args, cfg: dict) -> int:
    """原浏览器界面（--web），按需加载，命令行模式不付这份内存。"""
    import webbrowser
    from core import server

    if cfg.get("apk"):
        threading.Thread(target=lambda: (time.sleep(1.2), _autorun_web(cfg)),
                         daemon=True).start()
    httpd, port = server.serve(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"{C.APP_TITLE} {C.APP_VERSION}  →  {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while not server._STOP.is_set():
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
    return 0


def _autorun_web(cfg: dict) -> None:
    from core import server

    apk = Path(cfg.get("apk", ""))
    if not apk.exists():
        return
    if cfg.get("avd"):
        C.settings["avd"] = cfg["avd"]
    info = {"package": cfg.get("package", ""), "main_activity": cfg.get("activity", "")}
    action = "run" if info["package"] else "full"
    job = server.JOBS.start(action, {
        "apk": str(apk),
        "fullscreen": bool(cfg.get("fullscreen", False)),
        "info": info,
    })
    server.LAUNCH = {"job": job.id, "title": f"启动 {cfg.get('label', apk.stem)}"}


def detect_mode() -> str:
    """决定「双击、不带参数」时进哪个界面。

    优先级：APK2PC_MODE 环境变量 > exe 文件名后缀 > 默认 cli。
    源码态（python app.py）与旧启动器一律 cli，保证既有行为不变。
    """
    env = (os.environ.get("APK2PC_MODE") or "").strip().lower()
    if env in ("cli", "web", "gui", "select"):
        return env
    if C.is_frozen():
        stem = Path(sys.executable).stem.lower()
        if "-web" in stem:
            return "web"
        if "-gui" in stem:
            return "gui"
        if "-cli" in stem:
            return "cli"
        if "allinone" in stem or stem == "apk2pcstudio":
            return "select"
    return "cli"


def _gui_exe() -> Path | None:
    """三合一包里同目录的独立图形化版（GUI 版用单独的 Python 打包）。"""
    here = Path(sys.executable).parent
    for name in ("APK2PCStudio-GUI.exe", "*-GUI.exe"):
        hits = sorted(here.glob(name))
        if hits:
            return hits[0]
    return None


def start_gui(mode_args) -> int:
    """启动图形化版：本进程若带 tkinter 就直接开窗口，否则拉起独立 GUI exe。"""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        exe = _gui_exe()
        if not exe:
            print("未找到图形化版：请下载 APK2PCStudio-GUI.exe 放在同一目录。")
            _pause()
            return 1
        print(f"正在打开图形化版：{exe.name}")
        try:
            os.startfile(str(exe))  # type: ignore[attr-defined]
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"✘ 启动失败：{e}")
            _pause()
            return 1
    from gui_app import run_gui
    return run_gui(mode_args)


def run_select(args, cfg: dict) -> int:
    """三合一入口：让用户选一个界面，之后不再重复问。"""
    from core.cli import ask
    print("=" * 60)
    print(f"  {C.APP_TITLE} {C.APP_VERSION}  ·  选择界面")
    print("=" * 60)
    print("  1) 命令行向导   黑框菜单，最省资源（默认，直接回车）")
    print("  2) 浏览器界面   功能最全，会自动打开网页")
    print("  3) 图形窗口     像普通软件一样的窗口，能点按钮")
    print("  0) 退出")
    print("-" * 60)
    c = ask("请选择 [1]: ", "1").strip() or "1"
    if c == "0":
        return 0
    if c == "2":
        return run_web(args, cfg)
    if c == "3":
        return start_gui(args)
    from core.cli import run_cli
    return run_cli()


def main() -> int:
    ap = argparse.ArgumentParser(description="APK2PC Studio")
    ap.add_argument("--launch", help="直接运行指定 APK（模拟器内）")
    ap.add_argument("--web", action="store_true", help="使用浏览器界面（默认命令行）")
    ap.add_argument("--port", type=int, default=0, help="浏览器界面端口（默认随机）")
    ap.add_argument("--no-browser", action="store_true", help="浏览器界面下不自动打开浏览器")
    ap.add_argument("--headless-install", action="store_true", help="只装配工具链并注册 PATH 后退出")
    ap.add_argument("--cli", action="store_true", help="强制进入命令行向导")
    ap.add_argument("--gui", action="store_true", help="强制进入图形窗口界面")
    ap.add_argument("--select", action="store_true", help="显示界面选择菜单")
    args = ap.parse_args()

    if args.headless_install:
        from core.cli import log
        provision.ensure_apktool(log)
        try:
            provision.ensure_jadx(log)
        except Exception as e:  # noqa: BLE001
            log("warn", f"jadx: {e}")
        provision.ensure_buildtools(log)
        provision.ensure_emulator_runtime(log)
        provision.sync_tool_path(log)
        print("工具链就绪。", flush=True)
        return 0

    cfg = load_sidecar() or {}
    if args.launch:
        cfg = {**cfg, "apk": args.launch, "label": Path(args.launch).stem}

    # 启动器 / --launch：走轻量无人值守，不占端口、不开浏览器
    if cfg.get("apk") and not args.web and not args.cli and not args.gui and not args.select:
        return run_unattended(cfg)

    if args.select:
        return run_select(args, cfg)
    if args.gui:
        return start_gui(args)
    if args.web:
        return run_web(args, {} if args.cli else cfg)

    mode = "cli" if args.cli else detect_mode()
    if mode == "select":
        return run_select(args, cfg)
    if mode == "web":
        return run_web(args, cfg)
    if mode == "gui":
        return start_gui(args)
    from core.cli import run_cli
    return run_cli()


if __name__ == "__main__":
    _setup_console()
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已取消。")
        _pause()
        sys.exit(130)
    except BaseException:  # noqa: BLE001
        print("\n" + "=" * 60)
        print("启动失败，下面是原因（截图发给开发者即可）：")
        print("=" * 60)
        traceback.print_exc()
        _pause()
        sys.exit(1)
