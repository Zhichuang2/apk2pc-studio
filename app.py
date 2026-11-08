"""APK2PC Studio 启动器。

两种运行态：
1) 普通模式：起本地服务 + 打开浏览器 UI。
2) 单 APK 启动器模式：exe 旁挂 <名字>.apk2pc.json，双击直接「开机→装包→拉起全屏」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import config as C          # noqa: E402
from core import apk as apkmod         # noqa: E402
from core import launcher, pipeline, runtime, server  # noqa: E402


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


def autorun_handler(cfg: dict) -> None:
    """启动器模式：无人值守。已知包名就只装机+拉起，否则走完整流程。"""
    apk = Path(cfg.get("apk", ""))
    if not apk.exists():
        return
    if cfg.get("avd"):
        C.settings["avd"] = cfg["avd"]
    info = {"package": cfg.get("package", ""), "main_activity": cfg.get("activity", "")}
    action = "run" if info["package"] else "full"
    job = server.JOBS.start(action, {
        "apk": str(apk),
        "fullscreen": bool(cfg.get("fullscreen", True)),
        "info": info,
    })
    server.LAUNCH = {"job": job.id, "title": f"启动 {cfg.get('label', apk.stem)}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="APK2PC Studio")
    ap.add_argument("--launch", help="直接运行指定 APK（模拟器内）")
    ap.add_argument("--port", type=int, default=0, help="固定端口（默认随机）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--headless-install", action="store_true", help="只装配工具链后退出")
    args = ap.parse_args()

    if args.headless_install:
        from core import provision

        def log(level: str, text: str) -> None:
            print(f"[{level}] {text}", flush=True)

        provision.ensure_jadx(log)
        provision.ensure_buildtools(log)
        provision.ensure_emulator_runtime(log)
        return 0

    side = load_sidecar()
    cfg = side or {}
    if args.launch:
        cfg = {**cfg, "apk": args.launch, "label": Path(args.launch).stem}
    if cfg.get("apk"):
        threading.Thread(target=lambda: (time.sleep(1.2), autorun_handler(cfg)),
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


if __name__ == "__main__":
    sys.exit(main())
