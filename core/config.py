"""全局配置 / 工具链发现。

设计要点：
- 冻结成 EXE 后（PyInstaller onefile），只读资源在 sys._MEIPASS，可写数据在 %LOCALAPPDATA%。
- 所有外部工具（apktool / jadx / build-tools / adb / emulator）都通过统一的 discover() 暴露，
  找不到就交给 provision 模块按需下载，GUI 里显示为「缺失」。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

APP_TITLE = "APK2PC Studio"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------- 路径

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def res_root() -> Path:
    """只读资源根（web/、vendor/）。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """可写数据根。"""
    env = os.environ.get("APK2PC_HOME")
    if env:
        p = Path(env)
    else:
        p = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "APK2PCStudio"
    p.mkdir(parents=True, exist_ok=True)
    return p


DATA_DIR = data_dir()
TOOLS_DIR = DATA_DIR / "tools"
WORKSPACE = DATA_DIR / "workspace"
SDK_ROOT = DATA_DIR / "android-sdk"
KEYSTORE = DATA_DIR / "apk2pc-debug.keystore"
SETTINGS_FILE = DATA_DIR / "settings.json"
for _d in (TOOLS_DIR, WORKSPACE, SDK_ROOT):
    _d.mkdir(parents=True, exist_ok=True)

KEYSTORE_PASS = "android"
KEYSTORE_ALIAS = "apk2pc"

# ---------------------------------------------------------------- 下载源

# 官方源（dl.google.com 通常可达）
BUILD_TOOLS_ZIP = "https://dl.google.com/android/repository/build-tools_r34-windows.zip"
CMDLINE_TOOLS_ZIP = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
# apktool 也在 Maven 中央仓（apktool-cli 是全量 jar，java -jar 可直接跑），
# 所以 EXE 里不用再背 15 MB，启动解包时间直接砍掉一半
APKTOOL_VERSION = "3.0.3"
APKTOOL_JAR_URL = (f"https://repo1.maven.org/maven2/org/apktool/apktool-cli/"
                   f"{APKTOOL_VERSION}/apktool-cli-{APKTOOL_VERSION}.jar")
# GitHub 走加速镜像（github.com 直连在国内常被墙）
JADX_VERSION = "1.5.1"
JADX_URLS = [
    f"https://ghfast.top/https://github.com/skylot/jadx/releases/download/v{JADX_VERSION}/jadx-{JADX_VERSION}.zip",
    f"https://github.moeyy.xyz/https://github.com/skylot/jadx/releases/download/v{JADX_VERSION}/jadx-{JADX_VERSION}.zip",
    f"https://github.com/skylot/jadx/releases/download/v{JADX_VERSION}/jadx-{JADX_VERSION}.zip",
]

DEFAULT_SYSTEM_IMAGE = "system-images;android-34;google_apis;x86_64"
DEFAULT_AVD = "apk2pc_x86"


# ---------------------------------------------------------------- 设置

DEFAULTS = {
    "avd": DEFAULT_AVD,
    "system_image": DEFAULT_SYSTEM_IMAGE,
    "extra_apktool_args": "",
    # 不再强制 -no-snapshot-load：允许模拟器走快照快速开机（90s → 10s 级）
    "emu_extra_args": "-no-boot-anim",
    "quick_boot": True,
    "jadx_threads": 4,
    "last_apk": "",
    "auto_start_emulator": True,
}


class Settings(dict):
    def __init__(self) -> None:
        super().__init__(json.loads(json.dumps(DEFAULTS)))
        try:
            if SETTINGS_FILE.exists():
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except Exception:
            pass
        # 一次性迁移：老配置里写死了 -no-snapshot-load，会把每次开机都拖成冷启动
        args = str(self.get("emu_extra_args", ""))
        if "-no-snapshot-load" in args and self.get("quick_boot", True):
            rest = [a for a in args.split() if a != "-no-snapshot-load"]
            self["emu_extra_args"] = " ".join(rest) or "-no-boot-anim"
        try:
            if SETTINGS_FILE.exists():
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8") or "{}")
                if raw.get("emu_extra_args") != self.get("emu_extra_args"):
                    self.save()
        except Exception:
            pass

    def save(self) -> None:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(self, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass


settings = Settings()


# ---------------------------------------------------------------- 工具发现

def _first_existing(*paths) -> Path | None:
    for p in paths:
        if not p:
            continue
        try:
            path = Path(p)
        except Exception:
            continue
        if path.exists():
            return path
    return None


def java_exe() -> Path | None:
    p = shutil.which("java")
    if p:
        return Path(p)
    jh = os.environ.get("JAVA_HOME")
    if jh:
        cand = Path(jh) / "bin" / "java.exe"
        if cand.exists():
            return cand
    return _first_existing(
        r"C:\Program Files\Java\jdk-21\bin\java.exe",
        r"C:\Program Files\Java\jdk-17\bin\java.exe",
        r"C:\Program Files\Eclipse Adoptium\jdk-21\bin\java.exe",
    )


def keytool_exe() -> Path | None:
    j = java_exe()
    if j and (j.parent / "keytool.exe").exists():
        return j.parent / "keytool.exe"
    p = shutil.which("keytool")
    return Path(p) if p else None


_JAVA_HOME_CACHE: list[str] = []


def java_env() -> dict:
    """给 .bat 工具（apksigner/sdkmanager）用的干净 JAVA_HOME。

    很多机器上系统 JAVA_HOME 是坏的（指向 javapath 壳目录），
    这些 .bat 一进去就报 "JAVA_HOME is set to an invalid directory"，所以统一注入正确的。
    """
    home = java_home()
    return {"JAVA_HOME": home} if home else {}


def java_home() -> str:
    """真实 JDK 根目录（sdkmanager.bat 依赖 JAVA_HOME）。

    直接取 java.exe 的父目录是不行的：Windows 上常见的是
    C:\\Program Files\\Common Files\\Oracle\\Java\\javapath\\java.exe 这种转发壳。
    所以优先用环境变量，再问 JVM 自己（-XshowSettings:properties）。
    """
    if _JAVA_HOME_CACHE:
        return _JAVA_HOME_CACHE[0]
    env = os.environ.get("JAVA_HOME", "")
    if env and (Path(env) / "bin" / "java.exe").exists():
        _JAVA_HOME_CACHE.append(env)
        return env
    java = java_exe()
    if java:
        try:
            import subprocess
            p = subprocess.run([str(java), "-XshowSettings:properties", "-version"],
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in (p.stderr or "").splitlines():
                if "java.home" in line and "=" in line:
                    home = line.split("=", 1)[1].strip()
                    if home.endswith(os.sep + "jre"):
                        home = str(Path(home).parent)
                    if (Path(home) / "bin").exists():
                        _JAVA_HOME_CACHE.append(home)
                        return home
        except Exception:
            pass
    _JAVA_HOME_CACHE.append("")
    return ""


def apktool_jar() -> Path | None:
    return _first_existing(
        TOOLS_DIR / "apktool.jar",
        res_root() / "vendor" / "apktool.jar",   # 源码运行时才有；EXE 不再内置
    )


def jadx_exe() -> Path | None:
    return _first_existing(
        TOOLS_DIR / "jadx" / "bin" / "jadx.bat",
        TOOLS_DIR / "jadx" / "bin" / "jadx",
    )


def jadx_lib_dir() -> Path | None:
    """Maven 依赖树方式装出来的 jadx（lib/*.jar + jadx.cli.JadxCLI）。

    两个必要条件：cli 主 jar 在（依赖下了一半会 ClassNotFound），
    且没被自检打过 .broken 标记（缺 logback-core 那类问题会让 jadx 静默 rc=1）。
    """
    d = TOOLS_DIR / "jadx-lib"
    if not d.exists():
        return None
    if (d / ".broken").exists():
        return None
    if not list(d.glob("jadx-cli-*.jar")):
        return None
    return d


def jadx_base_cmd() -> list[str] | None:
    """返回调用 jadx 的命令前缀（不含业务参数）。"""
    java = java_exe()
    lib = jadx_lib_dir()
    if java and lib:
        return [str(java), "-cp", jadx_classpath(lib), "jadx.cli.JadxCLI"]
    ex = jadx_exe()
    if ex:
        if ex.suffix.lower() == ".bat":
            return ["cmd", "/c", str(ex)]
        return [str(ex)]
    jar = _first_existing(TOOLS_DIR / "jadx-cli.jar")
    if java and jar and jar.stat().st_size > 5 * 1024 * 1024:
        return [str(java), "-jar", str(jar)]
    return None


def androguard_cmd() -> list[str] | None:
    """纯 Python 反编译器（DAD），作为 jadx 不可用时的兜底。"""
    w = shutil.which("androguard")
    if w:
        return [w]
    cand = Path(sys.executable).parent / "Scripts" / "androguard.exe"
    if cand.exists():
        return [str(cand)]
    try:
        import importlib.util
        if importlib.util.find_spec("androguard") is not None:
            return [sys.executable, "-c",
                    "import sys;from androguard.cli.cli import entry_point;"
                    "sys.argv[0]='androguard';entry_point()"]
    except Exception:
        pass
    return None


def _ver_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v))


def jadx_classpath(lib: Path) -> str:
    """同一个 artifact 只保留最高版本。

    我们不是 Maven，没有 nearest-wins 版本仲裁；依赖树里出现
    slf4j-api 2.0.11 和 2.0.16 并存时，logback 会因为不匹配的 SPI 直接罢工，
    jadx 于是就「静默失败」（日志系统本身炸了，什么错都看不到）。
    """
    best: dict[str, tuple[tuple, Path]] = {}
    for jar in lib.glob("*.jar"):
        stem = jar.stem
        if "-" not in stem:
            continue
        artifact, ver = stem.rsplit("-", 1)
        key = _ver_key(ver)
        if artifact not in best or key > best[artifact][0]:
            best[artifact] = (key, jar)
    return os.pathsep.join(str(p) for _, p in sorted(best.values(), key=lambda x: x[1].name))


def java_decompiler() -> tuple[str, list[str]] | None:
    """('jadx'|'androguard', 命令前缀)"""
    j = jadx_base_cmd()
    if j:
        return ("jadx", j)
    a = androguard_cmd()
    if a:
        return ("androguard", a)
    return None


def _buildtools_dir() -> Path | None:
    base = TOOLS_DIR / "build-tools"
    if not base.exists():
        return None
    cands = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True)
    for c in cands:
        if (c / "apksigner.bat").exists() or (c / "apksigner").exists():
            return c
    return cands[0] if cands else None


def apksigner_exe() -> Path | None:
    bt = _buildtools_dir()
    if bt:
        f = _first_existing(bt / "apksigner.bat", bt / "apksigner")
        if f:
            return f
    return None


def zipalign_exe() -> Path | None:
    bt = _buildtools_dir()
    if bt:
        f = _first_existing(bt / "zipalign.exe", bt / "zipalign")
        if f:
            return f
    return shutil.which("zipalign")  # type: ignore[return-value]


def aapt2_exe() -> Path | None:
    bt = _buildtools_dir()
    if bt:
        f = _first_existing(bt / "aapt2.exe", bt / "aapt2")
        if f:
            return f
    return None


def adb_exe() -> Path | None:
    cands = [
        SDK_ROOT / "platform-tools" / "adb.exe",
        Path(r"D:\app\platform-tools-latest-windows\platform-tools\adb.exe"),
        Path(r"D:\app\APK一键安装工具_完整包\adb.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    ]
    f = _first_existing(*cands)
    if f:
        return f
    w = shutil.which("adb")
    return Path(w) if w else None


def emulator_exe() -> Path | None:
    return _first_existing(
        SDK_ROOT / "emulator" / "emulator.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "emulator" / "emulator.exe",
    )


def sdkmanager_exe() -> Path | None:
    return _first_existing(
        SDK_ROOT / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat",
        SDK_ROOT / "cmdline-tools" / "bin" / "sdkmanager.bat",
    )


def avdmanager_exe() -> Path | None:
    return _first_existing(
        SDK_ROOT / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat",
        SDK_ROOT / "cmdline-tools" / "bin" / "avdmanager.bat",
    )


def system_image_dir() -> Path | None:
    """默认系统镜像是否已下载。"""
    parts = settings["system_image"].split(";")
    if len(parts) != 4:
        return None
    _, tag, abi, arch = parts
    p = SDK_ROOT / "system-images" / tag / abi / arch
    return p if p.exists() else None


def avd_exists() -> bool:
    emu = emulator_exe()
    if not emu:
        return False
    avd_home = Path(os.environ.get("ANDROID_AVD_HOME") or (Path.home() / ".android" / "avd"))
    return (avd_home / (settings["avd"] + ".ini")).exists()


# ---------------------------------------------------------------- 环境快照

def env_snapshot() -> dict:
    java = java_exe()
    apktool = apktool_jar()
    dec = java_decompiler()
    bt = _buildtools_dir()
    sig = apksigner_exe()
    zipa = zipalign_exe()
    adb = adb_exe()
    emu = emulator_exe()
    img = system_image_dir()

    tools = [
        {"key": "java", "name": "Java 运行时", "required": True,
         "ok": bool(java), "path": str(java) if java else "",
         "hint": "需 JDK 11+（apktool 3.x 要求）", "installable": False},
        {"key": "apktool", "name": "apktool", "required": True,
         "ok": bool(apktool), "path": str(apktool) if apktool else "",
         "hint": "APK 解包 / 重建核心", "installable": False},
        {"key": "jadx", "name": "Java 反编译器", "required": False,
         "ok": bool(dec), "path": f"{dec[0]} · {dec[1][-1] if dec else ''}" if dec else "",
         "hint": "jadx 优先；拉不动时自动降级到 androguard（DAD）", "installable": True},
        {"key": "buildtools", "name": "Build-Tools（签名/对齐）", "required": False,
         "ok": bool(bt and sig and zipa), "path": str(bt) if bt else "",
         "hint": "zipalign + apksigner，重打包后必须签名才能安装", "installable": True},
        {"key": "adb", "name": "ADB", "required": False,
         "ok": bool(adb), "path": str(adb) if adb else "",
         "hint": "与模拟器/真机通信", "installable": False},
        {"key": "emulator", "name": "Android 模拟器", "required": False,
         "ok": bool(emu), "path": str(emu) if emu else "",
         "hint": "「在电脑上运行 APK」的运行时", "installable": True},
        {"key": "sysimg", "name": "系统镜像", "required": False,
         "ok": bool(img), "path": str(img) if img else "",
         "hint": settings["system_image"] + "（约 1.5 GB）", "installable": True},
    ]
    blocking = [t["name"] for t in tools if t["required"] and not t["ok"]]
    runnable = bool(java and apktool)
    return {
        "app": {"title": APP_TITLE, "version": APP_VERSION, "frozen": is_frozen()},
        "data_dir": str(DATA_DIR),
        "tools": tools,
        "blocking": blocking,
        "can_decode": runnable,
        "can_java": bool(dec),
        "can_build": bool(java and apktool and sig),
        "can_run": bool(adb and emu and img),
        "settings": dict(settings),
    }
