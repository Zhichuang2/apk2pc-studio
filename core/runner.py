"""子进程执行 / 下载 / 解压 公共设施。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Iterable

LogFn = Callable[[str, str], None]


def _no_window() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def win_bat(bat: Path, args: list[str]) -> list[str]:
    """.bat 不能直接被 CreateProcess 执行，必须经 cmd /c。"""
    if os.name != "nt":
        return [str(bat), *map(str, args)]
    line = subprocess.list2cmdline([str(bat), *[str(a) for a in args]])
    return ["cmd", "/c", line]


def run(cmd: Iterable[str], log: LogFn, cwd: Path | None = None,
        timeout: int | None = None, env: dict | None = None,
        stdin_text: str | None = None,
        cancel: threading.Event | None = None) -> int:
    """执行命令并把输出逐行喂给 log(level, text)。"""
    cmd = [str(c) for c in cmd]
    log("cmd", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_no_window(),
            env=full_env,
        )
    except FileNotFoundError as e:
        log("error", f"命令不存在: {cmd[0]} ({e})")
        return 127
    except Exception as e:  # noqa: BLE001
        log("error", f"启动失败: {e}")
        return 126

    if stdin_text is not None and proc.stdin:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except Exception:
            pass

    timer_hit = {"v": False}
    cancel_hit = {"v": False}

    def _killer() -> None:
        timer_hit["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    t = threading.Timer(timeout, _killer) if timeout else None
    if t:
        t.daemon = True
        t.start()

    # 取消监听：用户点「取消」时杀掉整个进程树
    def _watch_cancel() -> None:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                cancel_hit["v"] = True
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            time.sleep(0.3)

    if cancel is not None:
        threading.Thread(target=_watch_cancel, daemon=True).start()

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if line:
            log("out", line)
    rc = proc.wait()
    if t:
        t.cancel()
    if cancel_hit["v"]:
        log("warn", "命令已取消")
        return 130
    if timer_hit["v"]:
        log("error", f"命令超时（{timeout}s）已终止")
        return 124
    return rc


def start_detached(cmd: Iterable[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.Popen:
    """启动长期进程（模拟器），不接管输出，但重定向到文件避免管道阻塞。"""
    cmd = [str(c) for c in cmd]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    logfile = open(Path(cwd or ".") / "emulator-stdout.log", "ab") if cwd else subprocess.DEVNULL
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=logfile,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=_no_window() | (getattr(subprocess, "DETACHED_PROCESS", 0) if False else 0),
        env=full_env,
    )


def popen_capture(cmd: Iterable[str], timeout: int = 20) -> tuple[int, str]:
    """同步捕获输出（用于短命令：adb devices 等）。"""
    try:
        p = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_no_window(),
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


# ---------------------------------------------------------------- 下载

def http_exists(url: str, timeout: int = 15) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "APK2PC/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def download(url: str, dest: Path, log: LogFn, retries: int = 2,
             progress: bool = True, deadline: int = 900) -> Path:
    """带进度日志的下载，支持断点续传（.part）+ 整体超时（防止卡死在无响应镜像）。

    实现方式：每次尝试都丢到独立线程，主线程 join(deadline)；
    超时后不再等它，直接进入下一次尝试（线程是 daemon，不会拖住退出）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for attempt in range(1, retries + 2):
        box: dict = {}

        def _worker() -> None:
            try:
                box["p"] = _download_once(url, dest, log, attempt, progress)
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(deadline)
        if "p" in box:
            return box["p"]
        if "e" in box or not t.is_alive():
            last_err = str(box.get("e", "未知错误"))
            log("warn", f"下载失败: {last_err}")
        else:
            last_err = f"超过 {deadline}s 无响应"
            log("warn", f"下载卡住（{deadline}s），放弃该源")
    raise RuntimeError(f"下载失败: {url} :: {last_err}")


def _download_once(url: str, dest: Path, log: LogFn, attempt: int, progress: bool) -> Path:
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "APK2PC/1.0"}
    if have:
        headers["Range"] = f"bytes={have}-"
    log("info", f"下载 [{attempt}] {url}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        total = int(r.headers.get("Content-Length") or 0) + (have if r.status == 206 else 0)
        mode = "ab" if (have and r.status == 206) else "wb"
        if mode == "wb":
            have = 0
        done = have
        last_tick = 0.0
        with open(part, mode) as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if progress and now - last_tick > 1.0:
                    last_tick = now
                    pct = f"{done * 100 / total:.1f}%" if total else f"{done/1048576:.1f}MB"
                    log("progress", f"  {pct}  ({done/1048576:.1f} MB" +
                        (f" / {total/1048576:.1f} MB)" if total else ")"))
    if dest.exists():
        dest.unlink()
    part.rename(dest)
    log("ok" if not progress else "progress", f"完成 → {dest.name}"
        if not progress else f"  100%  ({dest.stat().st_size/1048576:.1f} MB)")
    return dest


def unzip(zip_path: Path, dest: Path, log: LogFn) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    log("info", f"解压 {zip_path.name} → {dest}")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    log("ok", "解压完成")
    return dest


def ensure_single_dir(dest: Path, log: LogFn) -> Path:
    """很多官方 zip 解压后只含一个顶层目录，把它提上来。"""
    entries = [p for p in dest.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


def find_files(root: Path, names: list[str]) -> Path | None:
    for n in names:
        for p in root.rglob(n):
            return p
    return None


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"
