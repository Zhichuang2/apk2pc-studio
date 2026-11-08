"""APK2PC Studio · 图形化版（tkinter 桌面窗口）。

设计要点：
- 与命令行版共用同一套 core（pipeline / provision / config），不复制业务逻辑；
- 所有耗时动作放后台线程，主线程只通过 after() 增量刷日志与进度，窗口不卡死；
- 装配工具时的「询问模拟器位置」用线程安全弹窗（队列 + Event），不会崩；
- 不用 print（--windowed 打包时 stdout 为 None），全部经 emit() 进日志队列。
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from core import apk as apkmod
from core import cli as climod
from core import config as C
from core import lightmode
from core import pipeline, provision

_TAGS = {
    "ok": ("#0a7f5a", "normal"),
    "warn": ("#b26a00", "normal"),
    "error": ("#c0392b", "normal"),
    "cmd": ("#2b6cb0", "normal"),
    "out": ("#6b7280", "normal"),
    "progress": ("#6b7280", "normal"),
    "step": ("#111827", "bold"),
    "info": ("#374151", "normal"),
}


class GuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.job: pipeline.Job | None = None
        self.stop = threading.Event()
        self.busy = False

        root.title(f"{C.APP_TITLE} {C.APP_VERSION} · 图形化版")
        root.geometry("880x640")
        root.minsize(760, 560)
        try:
            root.call("tk", "scaling", 1.15)
        except tk.TclError:
            pass

        self._build_ui()
        self.emit("ok", f"{C.APP_TITLE} {C.APP_VERSION} 已就绪")
        self.emit("info", f"成品目录：{C.output_dir()}")
        self.emit("info", "第一次使用会询问安卓模拟器位置并登记，之后所有版本共享、不会重复下载。")
        self.emit("info", "BETA 轻量运行时："
                         + ("开（全流程会附带评估）" if lightmode.enabled() else "关")
                         + "；点「BETA 轻量评估」可单独扫描，不启动模拟器。")
        root.after(100, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        r = self.root
        style = ttk.Style(r)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", padding=5)
        style.configure("Accent.TButton", foreground="#ffffff", background="#1c7f66")
        style.map("Accent.TButton", background=[("active", "#166653"), ("disabled", "#9fb8b0")])

        head = ttk.Frame(r, padding=(14, 12, 14, 6))
        head.pack(fill="x")
        ttk.Label(head, text="APK2PC Studio", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        ttk.Label(head, text=f"v{C.APP_VERSION}  ·  把 APK 变成能在电脑上跑的程序",
                  foreground="#6b7280").pack(side="left", padx=(10, 0))

        # APK 选择
        f1 = ttk.LabelFrame(r, text=" 1. 选择 APK ", padding=10)
        f1.pack(fill="x", padx=14, pady=4)
        self.apk_var = tk.StringVar(value=str(C.settings.get("last_apk") or ""))
        ttk.Entry(f1, textvariable=self.apk_var).pack(side="left", fill="x", expand=True)
        ttk.Button(f1, text="浏览…", width=9, command=self._pick_apk).pack(side="left", padx=(8, 0))

        # 输出目录
        f2 = ttk.LabelFrame(r, text=" 2. 成品输出目录（可切换） ", padding=10)
        f2.pack(fill="x", padx=14, pady=4)
        self.out_var = tk.StringVar(value=str(C.output_dir()))
        ttk.Entry(f2, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(f2, text="浏览…", width=9, command=self._pick_out).pack(side="left", padx=(6, 0))
        ttk.Button(f2, text="应用", width=7, command=self._apply_out).pack(side="left", padx=(6, 0))
        ttk.Button(f2, text="打开", width=7, command=self._open_out).pack(side="left", padx=(6, 0))

        # 功能按钮
        f3 = ttk.LabelFrame(r, text=" 3. 要做什么 ", padding=10)
        f3.pack(fill="x", padx=14, pady=4)
        self.actions = [
            ("一键跑起来", "full", True),
            ("只拆包 + Java", "decode", False),
            ("重新打包签名", "build", False),
            ("生成单文件启动器", "launcher", False),
            ("环境检查 / 补装", "env", False),
            ("清理无用数据", "clean", False),
            ("BETA 轻量评估", "light", False),
        ]
        self.buttons: list[ttk.Button] = []
        for i, (label, key, accent) in enumerate(self.actions):
            b = ttk.Button(f3, text=label, style="Accent.TButton" if accent else "TButton",
                           command=lambda k=key: self._start(k))
            b.grid(row=i // 3, column=i % 3, sticky="ew", padx=3, pady=3)
            self.buttons.append(b)
        for c in range(3):
            f3.columnconfigure(c, weight=1)

        # 进度
        f4 = ttk.Frame(r, padding=(14, 8, 14, 2))
        f4.pack(fill="x")
        self.step_var = tk.StringVar(value="空闲")
        ttk.Label(f4, textvariable=self.step_var).pack(side="left")
        self.cancel_btn = ttk.Button(f4, text="取消当前任务", width=14,
                                     command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.bar = ttk.Progressbar(f4, mode="determinate", maximum=100)
        self.bar.pack(fill="x", padx=(10, 10), expand=True, side="left")

        # 日志
        f5 = ttk.LabelFrame(r, text=" 运行日志 ", padding=(6, 6, 6, 6))
        f5.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        self.txt = tk.Text(f5, wrap="word", height=12, font=("Consolas", 10),
                           bg="#fbfcfd", relief="flat", state="disabled")
        sb = ttk.Scrollbar(f5, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        for lvl, (color, weight) in _TAGS.items():
            self.txt.tag_configure(lvl, foreground=color,
                                   font=("Consolas", 10, weight))

    # ------------------------------------------------------------ 队列
    def emit(self, level: str, text: str) -> None:
        self.q.put(("log", level, str(text)))

    def _drain(self) -> None:
        """主线程：把后台线程的消息刷到界面。"""
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append(item[1], item[2])
                elif kind == "progress":
                    self.bar["value"] = item[1]
                    if item[2]:
                        self.step_var.set(item[2])
                elif kind == "busy":
                    self._set_busy(item[1])
                elif kind == "done":
                    self._finish(item[1], item[2])
                elif kind == "ask":
                    _, prompt, default, box, ev = item
                    value = simpledialog.askstring("需要你确认", prompt,
                                                   initialvalue=default, parent=self.root)
                    box["value"] = default if value is None else value.strip().strip('"')
                    ev.set()
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _append(self, level: str, text: str) -> None:
        tag = level if level in _TAGS else "info"
        prefix = {"ok": "✔ ", "warn": "! ", "error": "✘ ", "cmd": "> ",
                  "step": "▶ "}.get(level, "  ")
        self.txt.configure(state="normal")
        self.txt.insert("end", prefix + text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _set_busy(self, on: bool) -> None:
        self.busy = on
        for b in self.buttons:
            b.configure(state="disabled" if on else "normal")
        self.cancel_btn.configure(state="normal" if on else "disabled")
        if not on:
            self.step_var.set("空闲")
            self.bar["value"] = 0

    # ------------------------------------------------------------ 交互
    def _pick_apk(self) -> None:
        p = filedialog.askopenfilename(title="选择 APK", filetypes=[("APK 文件", "*.apk"), ("所有文件", "*.*")])
        if p:
            self.apk_var.set(p)
            C.settings["last_apk"] = p
            C.settings.save()

    def _pick_out(self) -> None:
        p = filedialog.askdirectory(title="选择成品输出目录")
        if p:
            self.out_var.set(p)

    def _apply_out(self) -> None:
        raw = self.out_var.get().strip()
        if not raw:
            C.settings["output_dir"] = ""
        else:
            p = Path(raw)
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("目录不可用", str(e))
                return
            C.settings["output_dir"] = str(p.resolve())
        C.settings.save()
        self.out_var.set(str(C.output_dir()))
        self.emit("ok", f"成品目录已切换为：{C.output_dir()}")

    def _open_out(self) -> None:
        d = C.output_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            self.emit("error", f"打不开目录：{e}")

    def _cancel(self) -> None:
        self.stop.set()
        if self.job:
            self.job.cancel()
        self.emit("warn", "已发送取消指令，正在收尾…")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askokcancel("任务还在跑", "确定要关闭吗？后台任务会被取消。"):
            return
        self._cancel()
        self.root.destroy()

    # ------------------------------------------------------------ 任务
    def _apk(self) -> Path | None:
        raw = self.apk_var.get().strip().strip('"')
        if not raw:
            messagebox.showwarning("还没选 APK", "先点「浏览…」选一个 .apk 文件")
            return None
        p = Path(raw)
        if not p.exists() or p.suffix.lower() != ".apk":
            messagebox.showerror("文件不对", f"找不到或不是 .apk：{p}")
            return None
        C.settings["last_apk"] = str(p)
        C.settings.save()
        return p

    def _gui_ask(self, prompt: str, default: str = "") -> str:
        """后台线程调用 → 队列请求主线程弹窗并等待结果。"""
        box: dict = {}
        ev = threading.Event()
        self.q.put(("ask", prompt, default, box, ev))
        ev.wait(timeout=300)
        return box.get("value", default)

    def _start(self, key: str) -> None:
        if self.busy:
            return
        # tk 变量只能在主线程读：先取出字符串，再交给后台线程
        apk_raw = self.apk_var.get().strip().strip('"')
        # launcher / env / clean 不强求先选 APK（launcher 优先用上次签名成品）
        if key in ("full", "decode", "build", "light") and not apk_raw:
            messagebox.showwarning("还没选 APK", "先点「浏览…」选一个 .apk 文件")
            return
        if key in ("full", "decode", "build", "light") and apk_raw:
            p = Path(apk_raw)
            if not p.exists() or p.suffix.lower() != ".apk":
                messagebox.showerror("文件不对", f"找不到或不是 .apk：{p}")
                return
        self.stop = threading.Event()
        self.q.put(("busy", True))
        t = threading.Thread(target=self._worker, args=(key, apk_raw), daemon=True)
        t.start()

    def _worker(self, key: str, apk_raw: str) -> None:
        ok, result = False, {}
        try:
            if key == "env":
                self._env()
                ok = True
            elif key == "clean":
                self._clean()
                ok = True
            elif key == "launcher":
                ok, result = self._launcher(apk_raw)
            else:
                apk = Path(apk_raw)
                C.settings["last_apk"] = str(apk)
                C.settings.save()
                if key == "light":
                    # 纯静态评估：不装配工具链、不启动模拟器、不下载运行时
                    self.emit("info", "BETA 轻量运行时评估（纯静态扫描，不启动模拟器）…")
                    params = {"apk": str(apk)}
                    ws = self._last_workspace()
                    if ws:
                        params["decoded"] = str(ws)
                else:
                    self.emit("info", "检查工具链（已装过的直接复用，不会重复下载）…")
                    climod.ensure_tools(key, self.emit, askfn=self._gui_ask)
                    params = {"apk": str(apk)}
                    if key == "build":
                        params = self._build_params(apk)
                ok, result = self._run_job(key, params)
        except Exception as e:  # noqa: BLE001
            self.emit("error", f"{type(e).__name__}: {e}")
            self.emit("out", traceback.format_exc(limit=6))
        finally:
            self.q.put(("done", ok, result))

    def _build_params(self, apk: Path) -> dict:
        src = self._last_workspace()
        if not src:
            raise RuntimeError("没找到解包目录，请先执行「只拆包 + Java」")
        return {"decoded": str(src), "workspace": str(src.parent), "apk": str(apk)}

    @staticmethod
    def _last_workspace() -> Path | None:
        try:
            dirs = sorted((p for p in C.WORKSPACE.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for d in dirs:
                if (d / "src" / "apktool.yml").exists():
                    return d / "src"
        except OSError:
            pass
        return None

    def _run_job(self, action: str, params: dict) -> tuple[bool, dict]:
        jm = pipeline.JobManager()
        job = jm.start(action, params)
        self.job = job
        cursor = 0
        while True:
            snap = job.to_dict(cursor)
            for item in snap["logs"]:
                lvl, text = item.get("level", "info"), item.get("text", "")
                if lvl == "out" and text.startswith("["):
                    continue
                self.emit(lvl, text)
            cursor = snap["cursor"]
            p = snap["progress"]
            running = next((s["name"] for s in snap["steps"] if s["status"] == "running"), "")
            self.q.put(("progress", p["pct"], f"{p['done']}/{p['total']}  {running}"))
            if snap["status"] != "running":
                if snap["status"] == "ok":
                    self.emit("ok", f"完成，用时 {snap['elapsed']}s")
                elif snap.get("error"):
                    self.emit("error", f"失败：{snap['error']}")
                else:
                    self.emit("warn", f"结束（{snap['status']}）")
                for k, label in (("decoded", "解包目录"), ("java", "Java 源码"),
                                 ("artifact", "成品"), ("launcher", "独立启动器"),
                                 ("report", "BETA 评估报告"), ("report_json", "轻量清单")):
                    if snap["result"].get(k):
                        self.emit("info", f"{label}：{snap['result'][k]}")
                return snap["status"] == "ok", snap.get("result", {})
            if self.stop.wait(0.3):
                job.cancel()

    def _env(self) -> None:
        snap = C.env_snapshot()
        for t in snap["tools"]:
            self.emit("ok" if t["ok"] else "warn",
                      f"{'✔' if t['ok'] else '✘'} {t['name']}：{t['path'] or t['hint']}")
        # 只补轻量工具，不在这里拉 2 GB 模拟器（那是「一键跑起来」时才需要的）
        provision.ensure_apktool(self.emit)
        try:
            provision.ensure_jadx(self.emit)
        except Exception as e:  # noqa: BLE001
            self.emit("warn", f"jadx: {e}")
        provision.ensure_buildtools(self.emit)
        provision.sync_tool_path(self.emit)
        self.emit("ok", "工具链检查完成（安卓运行时会在真正运行 APK 时按需装配）")

    def _clean(self) -> None:
        freed, n = 0, 0
        try:
            for ws in C.WORKSPACE.iterdir():
                if not ws.is_dir():
                    continue
                for p in list(ws.glob("*.apk")) + list(ws.glob("*.idsig")):
                    name = p.name.lower()
                    if "-unsigned.apk" in name or "-aligned.apk" in name or name.endswith(".idsig"):
                        try:
                            freed += p.stat().st_size
                            p.unlink()
                            n += 1
                        except OSError:
                            pass
        except OSError as e:
            self.emit("warn", str(e))
        self.emit("ok", f"清理 {n} 项，释放约 {freed/1048576:.0f} MB（成品目录未动）")

    def _launcher(self, apk_raw: str = "") -> tuple[bool, dict]:
        last = str(C.settings.get("last_signed") or "")
        target = Path(last) if last and Path(last).exists() else (Path(apk_raw) if apk_raw else None)
        if not target or not target.exists():
            self.emit("error", "先跑一次「一键跑起来」生成重建签名的包，或选一个 APK")
            return False, {}
        info = apkmod.badging_info(target) or {"label": target.stem}
        label = apkmod.sanitize(info.get("label") or target.stem)
        out = C.output_dir() / label
        self.emit("info", f"生成单文件启动器（内嵌的是重建签名后的包）→ {out}")
        exe = pipeline.make_launcher(target, info, out)
        self.emit("ok", f"已生成：{exe}")
        return True, {"launcher": str(exe)}

    def _finish(self, ok: bool, result: dict) -> None:
        self.job = None
        self.q.put(("busy", False))
        if ok:
            self.emit("ok", "任务完成。")
        else:
            self.emit("warn", "任务未成功完成，看上面的日志。")


def run_gui(_args=None) -> int:
    root = tk.Tk()
    GuiApp(root)
    # 自检模式：打包后用来验证「窗口真的能初始化」，不必人工点开
    if os.environ.get("APK2PC_GUI_SELFTEST"):
        root.withdraw()
        root.after(400, root.destroy)
    root.mainloop()
    return 0


def main() -> int:
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
