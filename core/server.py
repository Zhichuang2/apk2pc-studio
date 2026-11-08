"""内置 HTTP 服务：静态 UI + JSON API。"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import apk as apkmod
from . import config as C
from . import pipeline, provision, runtime
from .runner import human_size, open_path, popen_capture

JOBS = pipeline.JobManager()
_STOP = threading.Event()
LAUNCH: dict | None = None      # 启动器模式下的自动任务信息


# ---------------------------------------------------------------- 系统对话框

def pick_path(kind: str) -> str:
    """借 PowerShell 的 WinForms 对话框拿绝对路径（tkinter 不可用时的稳妥方案）。"""
    if kind == "dir":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description='选择输出目录';"
            "if($d.ShowDialog() -eq 'OK'){[Console]::Out.Write($d.SelectedPath)}"
        )
    else:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$f=New-Object System.Windows.Forms.OpenFileDialog;"
            "$f.Filter='APK 文件 (*.apk)|*.apk|所有文件 (*.*)|*.*';"
            "$f.Title='选择要处理的 APK';"
            "if($f.ShowDialog() -eq 'OK'){[Console]::Out.Write($f.FileName)}"
        )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script],
                           capture_output=True, text=True, timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (p.stdout or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "APK2PC/" + C.APP_VERSION
    protocol_version = "HTTP/1.1"

    # -- 工具
    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bin(self, data: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def log_message(self, fmt, *args):  # noqa: A003
        # 设 APK2PC_DEBUG=1 才打印访问日志，平时保持安静
        if os.environ.get("APK2PC_DEBUG"):
            sys.stderr.write("[http] %s %s\n" % (self.command, self.path))
            sys.stderr.flush()

    # -- 路由
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/api/state":
            return self._json(self.state())
        if p == "/api/job":
            job = JOBS.get(q.get("id", ""))
            if not job:
                return self._json({"error": "job not found"}, 404)
            return self._json(job.to_dict(int(q.get("since") or 0)))
        if p == "/api/devices":
            return self._json({"devices": runtime.list_devices()})
        if p == "/api/screencap":
            serial = q.get("serial") or runtime.pick_serial() or ""
            data = runtime.screencap(serial) if serial else None
            if not data:
                return self._json({"error": "no device / capture failed"}, 404)
            return self._bin(data, "image/png")
        if p == "/api/open":
            target = q.get("path") or ""
            if target and Path(target).exists():
                open_path(Path(target))
                return self._json({"ok": True})
            return self._json({"error": "路径不存在"}, 400)
        return self._static(p)

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        p = u.path
        body = self._body()

        if p == "/api/job":
            action = body.get("action", "full")
            apk = body.get("apk", "")
            if action != "provision" and (not apk or not Path(apk).exists()):
                return self._json({"error": f"APK 不存在: {apk}"}, 400)
            params = {
                "apk": apk,
                "serial": body.get("serial"),
                "fullscreen": bool(body.get("fullscreen")),
                "workspace": body.get("workspace"),
                "decoded": body.get("decoded"),
                "info": body.get("info"),
                "target": body.get("target"),
            }
            if action == "provision":
                job = JOBS.start("provision", params)
            else:
                job = JOBS.start(action, params)
            C.settings["last_apk"] = apk
            C.settings.save()
            return self._json({"job": job.id, "title": job.title})

        if p == "/api/cancel":
            job = JOBS.get(body.get("id", ""))
            if job:
                job.cancel()
                return self._json({"ok": True})
            return self._json({"error": "job not found"}, 404)

        if p == "/api/pick":
            return self._json({"path": pick_path(body.get("kind", "file"))})

        if p == "/api/scan":
            apk = Path(body.get("apk", ""))
            if not apk.exists():
                return self._json({"error": "APK 不存在"}, 400)
            return self._json({"peek": apkmod.zip_peek(apk),
                               "size": human_size(apk.stat().st_size),
                               "badging": apkmod.badging_info(apk)})

        if p == "/api/emulator":
            act = body.get("action", "start")
            if act == "start":
                ok = runtime.start_emulator(_quiet, fullscreen=bool(body.get("fullscreen")))
                return self._json({"ok": ok, "booting": ok})
            if act == "stop":
                runtime.stop_all(_quiet)
                return self._json({"ok": True})
            return self._json({"error": "unknown action"}, 400)

        if p == "/api/export":
            kind = body.get("kind")
            job = JOBS.get(body.get("job") or "")
            res = (job.result if job else {}) or {}
            decoded = Path(res.get("decoded") or body.get("decoded") or "")
            java_dir = Path(res["java"]) if res.get("java") else None
            ws = Path(res.get("workspace") or C.WORKSPACE)
            info = res.get("info") or body.get("info") or {}
            if not decoded.exists():
                return self._json({"error": "没有解包结果，先跑一次解包"}, 400)
            try:
                if kind == "gradle":
                    out = pipeline.export_gradle(ws, decoded, java_dir, info)
                elif kind == "launcher":
                    # 交付的是「重建签名后的 APK」，不是把原包原样套回去
                    src_apk = Path(res.get("signed") or res.get("artifact")
                                   or res.get("apk") or body.get("apk") or "")
                    if not src_apk.exists():
                        return self._json({"error": "找不到用于打包的 APK，先跑一次重建签名"}, 400)
                    if not info.get("package"):
                        info = apkmod.badging_info(src_apk) or info
                    label = apkmod.sanitize(info.get("label") or src_apk.stem)
                    out_dir = Path(body.get("target_dir") or (C.output_dir() / label))
                    out = pipeline.make_launcher(src_apk, info, out_dir)
                else:
                    return self._json({"error": "unknown kind"}, 400)
                open_path(out if out.is_dir() else out.parent)
                return self._json({"ok": True, "path": str(out)})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        if p == "/api/settings":
            for k, v in body.items():
                if k in C.DEFAULTS:
                    C.settings[k] = v
            C.settings.save()
            return self._json({"ok": True, "settings": dict(C.settings)})

        if p == "/api/shutdown":
            _STOP.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)

    # -- 状态
    def state(self) -> dict:
        snap = C.env_snapshot()
        snap["devices"] = runtime.list_devices()
        snap["jobs"] = [
            {"id": j.id, "title": j.title, "status": j.status, "kind": j.kind}
            for j in (JOBS.get(i) for i in reversed(JOBS.order)) if j
        ][:12]
        snap["workspace"] = str(C.WORKSPACE)
        snap["output_dir"] = str(C.output_dir())
        snap["launch"] = LAUNCH
        return snap

    # -- 静态
    def _static(self, p: str) -> None:
        root = C.res_root() / "web"
        rel = "index.html" if p in ("/", "") else p.lstrip("/")
        target = (root / rel).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return self._json({"error": "forbidden"}, 403)
        if not target.exists() or target.is_dir():
            if rel != "index.html":
                return self._json({"error": "not found"}, 404)
            target = root / "index.html"
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return self._bin(target.read_bytes(), ctype)


def _quiet(level: str, text: str) -> None:
    pass


# ---------------------------------------------------------------- 启动

def serve(port: int = 0, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer((host, port), Handler)
    actual = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, actual
