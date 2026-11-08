"""Android 运行时管理：设备发现、模拟器启动、安装、拉起、截屏。"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from . import config as C
from .runner import LogFn, popen_capture


def _adb(*args: str, timeout: int = 30) -> tuple[int, str]:
    adb = C.adb_exe()
    if not adb:
        return -1, "未找到 adb"
    return popen_capture([adb, *args], timeout=timeout)


def list_devices() -> list[dict]:
    rc, out = _adb("devices", "-l")
    if rc != 0:
        return []
    devs = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        m = re.search(r"model:(\S+)", line)
        if m:
            model = m.group(1)
        kind = "emulator" if serial.startswith("emulator-") else "device"
        devs.append({"serial": serial, "state": state, "model": model, "kind": kind})
    return devs


def pick_serial(prefer: str | None = None) -> str | None:
    devs = [d for d in list_devices() if d["state"] == "device"]
    if not devs:
        return None
    if prefer:
        for d in devs:
            if d["serial"] == prefer:
                return prefer
    emu = [d for d in devs if d["kind"] == "emulator"]
    return (emu or devs)[0]["serial"]


_EMU_LOG: list[Path] = []        # 最近一次模拟器日志路径
_EMU_HANDLES: list = []          # 保持文件句柄存活


def last_emulator_log() -> Path | None:
    return _EMU_LOG[-1] if _EMU_LOG else None


def log_tail(path: Path | None, lines: int = 15) -> str:
    if not path or not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.strip().splitlines()[-lines:])
    except Exception:
        return ""


def start_emulator(log: LogFn, avd: str | None = None, extra: str = "",
                   fullscreen: bool = False, cold: bool = False) -> bool:
    emu = C.emulator_exe()
    if not emu:
        log("error", "未安装模拟器，请先点「装配运行时」")
        return False
    if list_devices():
        log("info", "已有设备在线，跳过模拟器启动")
        return True
    avd = avd or C.settings["avd"]
    # 默认允许读写快照：冷启动 ~90s，快照恢复 10s 级
    args = [str(emu), "-avd", avd, "-gpu", "swiftshader_indirect",
            "-netdelay", "none", "-netspeed", "full"]
    if cold:
        args.append("-no-snapshot-load")
    if fullscreen:
        args.append("-fullscreen")
    if C.settings.get("emu_extra_args"):
        args += C.settings["emu_extra_args"].split()
    if extra:
        args += extra.split()
    log("cmd", "启动模拟器: " + " ".join(args))

    # 模拟器自己的输出必须落盘：它秒退时，界面若只有「无设备」就等于瞎子
    logdir = C.DATA_DIR / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"emulator-{time.strftime('%m%d-%H%M%S')}.log"
    try:
        fh = open(logfile, "wb")
        _EMU_HANDLES.append(fh)
        proc = subprocess.Popen(
            args,
            cwd=str(emu.parent),
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # 独立进程组：主程序退出/崩溃时，模拟器不会被一起带走
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
        )
    except Exception as e:  # noqa: BLE001
        log("error", f"模拟器启动失败: {e}")
        return False

    _EMU_LOG.append(logfile)
    log("info", f"模拟器日志: {logfile}")

    # 秒退检测：正常启动要 1 分钟以上，2.5 秒内退出必然是失败
    time.sleep(2.5)
    if proc.poll() is not None:
        tail = log_tail(logfile, 12)
        log("error", f"模拟器启动后立即退出（code={proc.returncode}）")
        for line in tail.splitlines():
            log("out", line)
        return False
    return True


def wait_boot(log: LogFn, timeout: int = 300) -> str | None:
    """等设备出现并完成开机，返回 serial。"""
    deadline = time.time() + timeout
    serial = None
    last_note = 0.0
    while time.time() < deadline:
        devs = list_devices()
        ready = [d for d in devs if d["state"] == "device"]
        if ready:
            serial = ready[0]["serial"]
            rc, out = _adb("-s", serial, "shell", "getprop", "sys.boot_completed", timeout=15)
            if rc == 0 and "1" in out:
                log("ok", f"设备已就绪: {serial}")
                time.sleep(1.5)
                return serial
        if time.time() - last_note > 8:
            last_note = time.time()
            state = ", ".join(f"{d['serial']}({d['state']})" for d in devs) or "无设备"
            log("info", f"等待开机… 当前: {state}")
        time.sleep(2.0)
    log("error", "等待模拟器开机超时")
    return None


def install(apk: Path, serial: str, log: LogFn, pkg: str = "") -> bool:
    log("info", f"安装 {apk.name} → {serial}")
    rc, out = _adb("-s", serial, "install", "-r", "-t", "-g", str(apk), timeout=600)
    for line in out.splitlines():
        if line.strip():
            log("out", line.strip())
    if "Success" in out:
        log("ok", "安装成功")
        return True
    # 覆盖安装被系统拒绝的几类：签名冲突、版本降级、已存在不同签名。
    # 降级（VERSION_DOWNGRADE）在「用重建包覆盖原版」时很常见，必须卸载后重装。
    retry_marks = ("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "signatures do not match",
                   "INSTALL_FAILED_ALREADY_EXISTS", "INSTALL_FAILED_VERSION_DOWNGRADE",
                   "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES",
                   "INSTALL_FAILED_USER_RESTRICTED")
    if pkg and any(m in out for m in retry_marks):
        log("warn", f"系统拒绝覆盖安装，先卸载 {pkg} 再装")
        _adb("-s", serial, "uninstall", pkg, timeout=90)
        rc, out = _adb("-s", serial, "install", "-r", "-t", "-g", str(apk), timeout=600)
        log("out", out.strip())
        if "Success" in out:
            log("ok", "重装成功")
            return True
    # 没有包名时也允许降级安装（adb -d）
    if "INSTALL_FAILED_VERSION_DOWNGRADE" in out:
        rc, out = _adb("-s", serial, "install", "-r", "-d", "-t", "-g", str(apk), timeout=600)
        log("out", out.strip())
        if "Success" in out:
            return True
    log("error", "安装失败")
    return False


def uninstall(pkg: str, serial: str, log: LogFn) -> None:
    rc, out = _adb("-s", serial, "uninstall", pkg, timeout=90)
    log("out", out.strip() or f"uninstall rc={rc}")


def launch(pkg: str, activity: str, serial: str, log: LogFn) -> bool:
    if activity:
        comp = activity if "/" in activity else f"{pkg}/{activity}"
        rc, out = _adb("-s", serial, "shell", "am", "start", "-n", comp, timeout=60)
        log("out", out.strip())
        if rc == 0 and "Error" not in out:
            log("ok", f"已启动 {comp}")
            return True
        log("warn", "am start 失败，改用 monkey 兜底")
    rc, out = _adb("-s", serial, "shell", "monkey", "-p", pkg,
                   "-c", "android.intent.category.LAUNCHER", "1", timeout=60)
    log("out", out.strip()[:800])
    ok = rc == 0 and "No activities found" not in out
    log("ok" if ok else "error", "已拉起应用" if ok else "拉起失败")
    return ok


def screencap(serial: str) -> bytes | None:
    adb = C.adb_exe()
    if not adb:
        return None
    try:
        p = subprocess.run([str(adb), "-s", serial, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=25,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode == 0 and p.stdout[:4] == b"\x89PNG":
            return p.stdout
    except Exception:
        pass
    return None


def stop_all(log: LogFn) -> None:
    rc, out = _adb("emu", "kill", timeout=30)
    log("out", out.strip() or f"rc={rc}")


def package_running(pkg: str, serial: str) -> bool:
    rc, out = _adb("-s", serial, "shell", "pidof", pkg, timeout=15)
    return rc == 0 and out.strip() != ""


def device_detail(serial: str) -> dict:
    detail = {"serial": serial, "model": "", "android": "", "abi": ""}
    for key, prop in (("model", "ro.product.model"), ("android", "ro.build.version.release"),
                      ("abi", "ro.product.cpu.abi")):
        rc, out = _adb("-s", serial, "shell", "getprop", prop, timeout=12)
        if rc == 0:
            detail[key] = out.strip()
    return detail
