"""用户级环境变量 / PATH：运行时只装一次，所有产物共享，绝不重复下载。

规则（对应需求）：
1. 先查 PATH / APK2PC_RUNTIME 里登记的运行时位置；
2. 有旧记录但按它找不到 emulator.exe → 删掉旧记录，重新询问；
3. 找不到 → 询问用户「模拟器在哪」；没装 → 询问「装在哪」；
4. 确定位置后写入用户级环境变量并注册 PATH（免管理员）。

Windows 上写 HKCU\\Environment，广播 WM_SETTINGCHANGE，并同步当前进程 os.environ。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .runner import LogFn

_MARK = "APK2PCStudio"
RUNTIME_VAR = "APK2PC_RUNTIME"
TOOLPATHS_VAR = "APK2PC_TOOLPATHS"   # 精确记录「我们加进 PATH 的目录」，与目录名无关
OUTPUT_VAR = "APK2PC_OUTPUT"
_DIRS: list[Path] | None = None


def _noop(_level: str, _text: str) -> None:
    pass


def _env_key(write: bool = False):
    import winreg

    access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
    return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, access)


def get_var(name: str) -> str:
    if sys.platform != "win32":
        return os.environ.get(name, "")
    import winreg

    try:
        with _env_key() as k:
            val, _ = winreg.QueryValueEx(k, name)
            return str(val)
    except (OSError, FileNotFoundError):
        return os.environ.get(name, "")


def set_var(name: str, value: str) -> None:
    os.environ[name] = value
    if sys.platform != "win32":
        return
    import winreg

    try:
        with _env_key(write=True) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_EXPAND_SZ, value)
        _broadcast()
    except OSError:
        pass


def del_var(name: str) -> None:
    os.environ.pop(name, None)
    if sys.platform != "win32":
        return
    import winreg

    try:
        with _env_key(write=True) as k:
            winreg.DeleteValue(k, name)
        _broadcast()
    except OSError:
        pass


def _read_path() -> tuple[list[str], int]:
    if sys.platform != "win32":
        return [], 1
    import winreg

    try:
        with _env_key() as k:
            try:
                val, typ = winreg.QueryValueEx(k, "Path")
            except FileNotFoundError:
                return [], winreg.REG_EXPAND_SZ
        return [x for x in str(val).split(";") if x.strip()], typ
    except OSError:
        return [], winreg.REG_EXPAND_SZ


def _write_path(items: list[str], typ: int) -> bool:
    import winreg

    try:
        with _env_key(write=True) as k:
            winreg.SetValueEx(k, "Path", 0, typ, ";".join(items))
        return True
    except OSError:
        return False


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p))).rstrip("\\/")


def _broadcast() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, ctypes.byref(result))
    except Exception:
        pass


def tool_dirs() -> list[Path]:
    """已登记的工具目录（进程内只解析一次）。

    不靠路径里有没有 APK2PCStudio 字样判断：用户可能把模拟器装在 D:\\MyEmu，
    那样按名字过滤会漏掉，导致每次运行都重新询问 / 重复下载。
    """
    global _DIRS
    if _DIRS is not None:
        return _DIRS
    raw = get_var(TOOLPATHS_VAR)
    cands: list[str] = [x for x in raw.split(";") if x.strip()] if raw else []
    if not cands:  # 兼容旧版本：从 PATH 里带标记的项回推
        items, _ = _read_path()
        cands = [x for x in items if _MARK.lower() in x.lower()]
    out: list[Path] = []
    for item in cands:
        p = Path(os.path.expandvars(item))
        if p.is_dir() and _norm(p) not in {_norm(x) for x in out}:
            out.append(p)
    _DIRS = out
    return out


def find(name: str) -> Path | None:
    """从已注册的工具目录里找可执行文件：命中就不必再下载。"""
    for d in tool_dirs():
        for suffix in ("", ".exe", ".bat", ".cmd", ".jar"):
            p = d / (name + suffix)
            if p.is_file():
                return p
    return None


def register(dirs: list[Path], log: LogFn | None = None) -> int:
    """把已存在的工具目录追加进用户 PATH，返回新增数量。幂等。"""
    global _DIRS
    log = log or _noop
    if sys.platform != "win32":
        return 0
    existed, typ = _read_path()
    seen = {_norm(x) for x in existed}
    added: list[str] = []
    for d in dirs:
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        s = str(d)
        if _norm(s) not in seen:
            added.append(s)
            seen.add(_norm(s))
    if not added:
        # 目录本来就在 PATH（用户手动加过 / 旧版本留下的）：补记一份，
        # 否则下次按标记过滤会漏掉自定义目录，又变成重复询问。
        _record([str(d) for d in dirs if d.is_dir()])
        _DIRS = None
        log("info", "PATH 已包含全部工具目录，无需改动")
        return 0
    if _write_path([x for x in existed if x.strip()] + added, typ):
        os.environ["PATH"] = os.environ.get("PATH", "") + ";" + ";".join(added)
        _record(added)
        _DIRS = None
        _broadcast()
        log("ok", f"已加入用户 PATH（{len(added)} 个目录），下次运行直接复用")
        return len(added)
    os.environ["PATH"] = os.environ.get("PATH", "") + ";" + ";".join(added)
    _record(added)
    _DIRS = None
    log("warn", "写入 PATH 失败（本次运行仍可用，重启后可能失效）")
    return 0


def _record(added: list[str]) -> None:
    """把新加的目录记进 TOOLPATHS_VAR，供下次精确复用 / 清理。"""
    old = [x for x in get_var(TOOLPATHS_VAR).split(";") if x.strip()]
    seen = {_norm(x) for x in old}
    merged = old + [x for x in added if _norm(x) not in seen]
    set_var(TOOLPATHS_VAR, ";".join(merged))


def prune_stale(log: LogFn | None = None) -> int:
    """删除已登记但目录不存在的旧记录（PATH + TOOLPATHS_VAR 同步清）。"""
    global _DIRS
    log = log or _noop
    if sys.platform != "win32":
        return 0
    recorded = [x for x in get_var(TOOLPATHS_VAR).split(";") if x.strip()]
    if not recorded:
        return 0
    gone = {_norm(x) for x in recorded
            if not Path(os.path.expandvars(x)).is_dir()}
    if not gone:
        return 0
    kept = [x for x in recorded if _norm(x) not in gone]
    set_var(TOOLPATHS_VAR, ";".join(kept))
    items, typ = _read_path()
    _write_path([x for x in items if x.strip() and _norm(x) not in gone], typ)
    _DIRS = None
    _broadcast()
    log("warn", f"已清除 {len(gone)} 条失效的旧路径记录")
    return len(gone)


def _has_emulator(root: Path) -> bool:
    return (root / "emulator" / "emulator.exe").is_file() or \
           (root / "emulator.exe").is_file() or \
           (root / "platform-tools" / "adb.exe").is_file()


def _probe(root: Path) -> bool:
    """用户给的路径可能指向 sdk 根 / emulator 目录 / 任意上级，向上找两层。"""
    for i in range(3):
        if _has_emulator(root):
            return True
        if i < 2:
            for sub in ("emulator", "Sdk", "android-sdk"):
                if _has_emulator(root / sub):
                    return True
        root = root.parent
        if root == root.parent:
            break
    return False


def resolve_runtime(ask, log: LogFn | None = None, install_root: Path | None = None):
    """返回 (运行时根目录, 是否需要安装)。

    ask(prompt, default) -> str，由调用方（命令行向导）提供；
    非交互环境返回 None 时自动用默认目录安装。
    """
    log = log or _noop
    prune_stale(log)

    recorded = get_var(RUNTIME_VAR).strip()
    if recorded:
        p = Path(os.path.expandvars(recorded))
        if _probe(p):
            log("ok", f"复用已登记的运行时：{p}")
            return p, False
        log("warn", f"旧的运行时记录已失效（找不到模拟器）：{p}")
        del_var(RUNTIME_VAR)
        del_var(TOOLPATHS_VAR)       # 旧登记一并作废，重新询问
        prune_stale(log)

    # 再从 PATH 已注册目录推一次（兼容只注册过 PATH 的旧版本）
    for d in tool_dirs():
        candidate = d.parent if d.name.lower() in ("platform-tools", "emulator", "bin") else d
        if _probe(candidate):
            set_var(RUNTIME_VAR, str(candidate))
            log("ok", f"从 PATH 找到运行时：{candidate}")
            return candidate, False

    if ask is None:
        root = install_root or Path(os.environ.get("LOCALAPPDATA", ".")) / _MARK / "android-sdk"
        set_var(RUNTIME_VAR, str(root))
        return root, True

    log("warn", "没有检测到安卓模拟器位置")
    print("\n  请选择：")
    print("   1) 我已经装了模拟器 / Android SDK，告诉你目录")
    print("   2) 还没装，让我选一个安装位置（程序会自动下载）")
    choice = ask("  输入 1 或 2 [2]：", "2")
    if str(choice).strip() == "1":
        raw = ask("  模拟器所在目录（可直接把文件夹拖进来）：", "")
        if not raw:
            return None, False
        p = Path(raw.strip('"').strip())
        if not _probe(p):
            print(f"  ✘ 在 {p} 附近没找到 emulator.exe / adb.exe")
            raw2 = ask("  再试一次，或输入 2 改为安装 [2]：", "2")
            if str(raw2).strip() == "2":
                return _ask_install(ask, install_root)
            return None, False
        # 归一到含 emulator/ 的那一层
        root = p
        for _ in range(3):
            if (root / "emulator" / "emulator.exe").is_file() or \
               (root / "platform-tools" / "adb.exe").is_file():
                break
            root = root.parent
        set_var(RUNTIME_VAR, str(root))
        register([root / "platform-tools", root / "emulator", root / "cmdline-tools" / "latest" / "bin"], log)
        print(f"  ✔ 已记录并加入 PATH：{root}")
        return root, False
    return _ask_install(ask, install_root)


def _ask_install(ask, install_root: Path | None):
    default = str(install_root or Path(os.environ.get("LOCALAPPDATA", ".")) / _MARK / "android-sdk")
    raw = ask(f"  装在哪？（直接回车用默认位置）\n  [{default}]\n  > ", default)
    root = Path(raw.strip('"').strip() or default)
    root.mkdir(parents=True, exist_ok=True)
    set_var(RUNTIME_VAR, str(root))
    print(f"  ✔ 将安装到：{root}")
    return root, True


def sync_tool_path(log: LogFn | None = None, extra: list[Path] | None = None) -> int:
    """把现有工具目录补进 PATH。由 provision 在装配完成后调用。"""
    from . import config as C

    dirs: list[Path] = [C.TOOLS_DIR]
    sdk = C.sdk_root()
    dirs += [sdk / "platform-tools", sdk / "emulator",
             sdk / "cmdline-tools" / "latest" / "bin", sdk / "cmdline-tools" / "bin"]
    bt = C._buildtools_dir()
    if bt:
        dirs.append(bt)
    jadx = C.TOOLS_DIR / "jadx" / "bin"
    if jadx.is_dir():
        dirs.append(jadx)
    if extra:
        dirs += extra
    set_var(RUNTIME_VAR, str(sdk))
    return register(dirs, log)
