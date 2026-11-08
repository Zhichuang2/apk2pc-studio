"""工具链 / 运行时按需装配。

全部走「缺失才下载」，下载源见 config；模拟器走官方 cmdline-tools + sdkmanager，
这样 license、目录结构、AVD 都由 Google 自己的工具管理，比手搓 config.ini 稳。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import config as C
from .runner import LogFn, download, ensure_single_dir, find_files, popen_capture, run, unzip


# ---------------------------------------------------------------- apktool

def ensure_apktool(log: LogFn) -> Path:
    jar = C.apktool_jar()
    if jar:
        if jar.parent != C.TOOLS_DIR:
            C.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            dst = C.TOOLS_DIR / "apktool.jar"
            if not dst.exists():
                shutil.copy2(jar, dst)
                log("ok", f"apktool 已纳入工具目录: {dst}")
            return dst
        return jar

    # 本地没有 → 从 Maven 中央仓取（apktool-cli 是全量 jar，国内速度好）
    log("info", f"下载 apktool {C.APKTOOL_VERSION}（约 23 MB，Maven 中央仓）")
    dst = C.TOOLS_DIR / "apktool.jar"
    download(C.APKTOOL_JAR_URL, dst, log, deadline=240)
    if not dst.exists() or dst.stat().st_size < 5 * 1024 * 1024:
        raise RuntimeError("apktool 下载不完整")
    log("ok", f"apktool 就绪: {dst}")
    return dst


# ---------------------------------------------------------------- Java 反编译器

def _ensure_jadx_companions(log: LogFn) -> None:
    """补齐几个「少了就静默失败」的运行期伴生 jar。

    logback-core 缺失时 logback 的 SPI 无法实例化 → slf4j 静默退回 NOP →
    jadx 的报错全部被吞掉，表现为「rc=1 且没有任何输出」。真实踩过。
    """
    lib = C.TOOLS_DIR / "jadx-lib"
    if not lib.exists():
        return

    def _ver_of(prefix: str) -> str:
        for p in sorted(lib.glob(prefix + "-*.jar")):
            return p.stem[len(prefix) + 1:]
        return ""

    lc_ver = _ver_of("logback-classic")
    if lc_ver and not _ver_of("logback-core"):
        target = lib / f"logback-core-{lc_ver}.jar"
        log("info", f"补装 logback-core:{lc_ver}（缺失会让 jadx 静默失败）")
        try:
            download(f"https://repo1.maven.org/maven2/ch/qos/logback/logback-core/{lc_ver}"
                     f"/logback-core-{lc_ver}.jar", target, log, retries=2, progress=False)
        except Exception as e:  # noqa: BLE001
            log("warn", f"logback-core 安装失败: {e}")


def _verify_jadx(log: LogFn) -> bool:
    """装配后自检：能打印版本号才算真的能用。

    只检查文件存在是不够的 —— 依赖树少一个 jar 时 jadx 会 rc=1 且**零输出**，
    这种「装好了但一跑就废」的状态必须先治，否则用户只会看到
    「反编译为 Java 源码 无产出」。
    """
    base = C.jadx_base_cmd()
    if not base:
        return False
    rc, out = popen_capture(base + ["--version"], timeout=120)
    tail = (out or "").strip().splitlines()[-1][:200] if (out or "").strip() else ""
    if rc == 0 and C.JADX_VERSION.split(".")[0] in (out or ""):
        return True
    log("warn", f"jadx 自检失败（rc={rc}）{('：' + tail) if tail else ''}")
    return False


def _mark_jadx_broken(log: LogFn, reason: str) -> None:
    lib = C.TOOLS_DIR / "jadx-lib"
    if lib.exists():
        try:
            (lib / ".broken").write_text(reason, encoding="utf-8")
        except Exception:
            pass
    log("warn", f"jadx 依赖树标记为不可用（{reason}），将改用备用方案")


def ensure_jadx(log: LogFn) -> None:
    """优先走 Maven 中央仓的依赖树（国内速度好），失败再试 GitHub 镜像 zip。"""
    if C.jadx_base_cmd():
        return
    from . import maven

    try:
        maven.fetch_tree(("io.github.skylot", "jadx-cli", C.JADX_VERSION),
                         C.TOOLS_DIR / "jadx-lib", log, workers=8)
        _ensure_jadx_companions(log)
        if _verify_jadx(log):
            log("ok", "jadx 就绪（Maven 依赖树 · 自检通过）")
            return
        _mark_jadx_broken(log, "自检未通过")
    except Exception as e:  # noqa: BLE001
        log("warn", f"Maven 方式失败: {e}")

    zip_path = C.TOOLS_DIR / f"jadx-{C.JADX_VERSION}.zip"
    err: Exception | None = None
    for url in C.JADX_URLS:
        try:
            download(url, zip_path, log, deadline=300)
            err = None
            break
        except Exception as e:  # noqa: BLE001
            err = e
            log("warn", "该镜像不可用，换下一个…")
    if err or not zip_path.exists():
        raise RuntimeError(f"jadx 下载失败: {err}")
    tmp = C.TOOLS_DIR / "_jadx_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    unzip(zip_path, tmp, log)
    inner = ensure_single_dir(tmp, log)
    target = C.TOOLS_DIR / "jadx"
    shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(inner), str(target))
    shutil.rmtree(tmp, ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    if not C.jadx_base_cmd():
        raise RuntimeError("jadx 解压后仍无法调用")
    log("ok", "jadx 就绪（官方 zip）")


def pip_install_androguard(log: LogFn) -> None:
    """纯 Python 反编译器，pip 装，作为 jadx 的兜底。"""
    mirrors = [
        [],
        ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"],
        ["-i", "https://mirrors.aliyun.com/pypi/simple"],
    ]
    for extra in mirrors:
        log("info", "pip 安装 androguard" + (f"（{extra[-1]}）" if extra else ""))
        rc = run([sys.executable, "-m", "pip", "install", "-q",
                  "--disable-pip-version-check", "androguard", *extra], log, timeout=900)
        if rc == 0 and C.androguard_cmd():
            log("ok", "androguard 就绪")
            return
    raise RuntimeError("androguard 安装失败")


def ensure_java_decompiler(log: LogFn) -> tuple[str, list[str]]:
    """保证至少有一个能出 Java 的反编译器。"""
    cur = C.java_decompiler()
    if cur:
        return cur
    try:
        ensure_jadx(log)
    except Exception as e:  # noqa: BLE001
        log("warn", f"jadx 不可用: {e}")
    cur = C.java_decompiler()
    if cur:
        return cur
    log("warn", "降级到 androguard（DAD 反编译，代码质量略低于 jadx）")
    pip_install_androguard(log)
    cur = C.java_decompiler()
    if not cur:
        raise RuntimeError("没有任何可用的 Java 反编译器")
    return cur


# ---------------------------------------------------------------- build-tools

def ensure_buildtools(log: LogFn) -> Path:
    if C.apksigner_exe() and C.zipalign_exe():
        return C.apksigner_exe().parent  # type: ignore[union-attr]
    zip_path = C.TOOLS_DIR / "build-tools_r34-windows.zip"
    if not zip_path.exists():
        download(C.BUILD_TOOLS_ZIP, zip_path, log)
    tmp = C.TOOLS_DIR / "_bt_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    unzip(zip_path, tmp, log)
    inner = ensure_single_dir(tmp, log)
    target_root = C.TOOLS_DIR / "build-tools"
    target_root.mkdir(parents=True, exist_ok=True)
    ver_dir = target_root / inner.name
    if ver_dir.exists():
        shutil.rmtree(ver_dir, ignore_errors=True)
    shutil.move(str(inner), str(ver_dir))
    shutil.rmtree(tmp, ignore_errors=True)

    # zip 内的顶层名可能是 android-14 之类，规范成版本号目录名
    if ver_dir.name.startswith("android-"):
        api = ver_dir.name.split("-")[1]
        newname = {"14": "34.0.0", "13": "33.0.0", "12": "31.0.0"}.get(api, ver_dir.name)
        renamed = target_root / newname
        if not renamed.exists():
            ver_dir.rename(renamed)
            ver_dir = renamed

    if not C.apksigner_exe():
        raise RuntimeError("build-tools 装配后仍未找到 apksigner")
    log("ok", f"build-tools 就绪: {ver_dir}")
    return ver_dir


# ---------------------------------------------------------------- cmdline-tools / SDK

def _java_env() -> dict:
    home = C.java_home()
    return {"JAVA_HOME": home} if home else {}


def ensure_sdkmanager(log: LogFn) -> Path:
    ex = C.sdkmanager_exe()
    if ex:
        return ex
    zip_path = C.TOOLS_DIR / "commandlinetools-win.zip"
    if not zip_path.exists():
        download(C.CMDLINE_TOOLS_ZIP, zip_path, log)
    tmp = C.SDK_ROOT / "_clt_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    unzip(zip_path, tmp, log)
    inner = ensure_single_dir(tmp, log)  # tmp/cmdline-tools
    latest = C.SDK_ROOT / "cmdline-tools" / "latest"
    if latest.exists():
        shutil.rmtree(latest, ignore_errors=True)
    latest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(inner), str(latest))
    shutil.rmtree(tmp, ignore_errors=True)
    ex = C.sdkmanager_exe()
    if not ex:
        raise RuntimeError("cmdline-tools 装配失败")
    log("ok", f"sdkmanager 就绪: {ex}")
    return ex


def accept_licenses(log: LogFn) -> None:
    sm = ensure_sdkmanager(log)
    log("info", "接受 Android SDK 许可协议…")
    rc = run([sm, f"--sdk_root={C.SDK_ROOT}", "--licenses"], log,
             stdin_text=("y\n" * 40), env=_java_env(), timeout=300)
    if rc != 0:
        log("warn", f"--licenses 返回 {rc}（一般无害，继续）")


def install_sdk_packages(packages: list[str], log: LogFn) -> None:
    sm = ensure_sdkmanager(log)
    accept_licenses(log)
    args = [sm, f"--sdk_root={C.SDK_ROOT}"] + packages
    log("info", "sdkmanager 安装: " + " ".join(packages))
    rc = run(args, log, stdin_text=("y\n" * 40), env=_java_env(), timeout=3600)
    if rc != 0:
        raise RuntimeError(f"sdkmanager 安装失败（rc={rc}）")
    log("ok", "SDK 组件安装完成")


def ensure_emulator_runtime(log: LogFn) -> Path:
    """emulator.exe + platform-tools + 系统镜像 + AVD，一次配齐。"""
    emu = C.emulator_exe()
    img_ok = C.system_image_dir() is not None
    if not (emu and img_ok and C.adb_exe()):
        pkgs = ["platform-tools", "emulator", C.settings["system_image"]]
        install_sdk_packages(pkgs, log)
    emu = C.emulator_exe()
    if not emu:
        raise RuntimeError("模拟器安装后仍未找到 emulator.exe")
    if not C.avd_exists():
        create_avd(log)
    log("ok", f"运行时就绪: {emu}")
    return emu


def create_avd(log: LogFn) -> None:
    avdm = C.avdmanager_exe()
    if not avdm:
        raise RuntimeError("未找到 avdmanager")
    log("info", f"创建 AVD: {C.settings['avd']}")
    rc = run(
        [avdm, "create", "avd", "-n", C.settings["avd"], "-k", C.settings["system_image"],
         "-d", "pixel_5", "--force"],
        log, stdin_text="no\n", env=_java_env(), timeout=300,
    )
    if rc != 0:
        raise RuntimeError(f"AVD 创建失败（rc={rc}）")
    _tune_avd(log)


def _tune_avd(log: LogFn) -> None:
    avd_home = Path(os.environ.get("ANDROID_AVD_HOME") or (Path.home() / ".android" / "avd"))
    cfg = avd_home / (C.settings["avd"] + ".avd") / "config.ini"
    if not cfg.exists():
        return
    wanted = {
        "hw.ramSize": "4096",
        "hw.keyboard": "yes",
        "hw.gpu.enabled": "yes",
        "hw.gpu.mode": "swiftshader_indirect",
        # 6G 装不下 2 GB 级的手游（Phigros 2.47G 会直接 INSUFFICIENT_STORAGE）
        "disk.dataPartition.size": "8G",
        "showDeviceFrame": "no",
    }
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        kv = {}
        for ln in lines:
            if "=" in ln and not ln.strip().startswith("#"):
                k, v = ln.split("=", 1)
                kv[k.strip()] = v.strip()
        kv.update(wanted)
        cfg.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n", encoding="utf-8")
        log("ok", "AVD 参数已调优（RAM 4G / swiftshader）")
    except Exception as e:  # noqa: BLE001
        log("warn", f"AVD 调优跳过: {e}")


def accel_check(log: LogFn) -> str:
    emu = C.emulator_exe()
    if not emu:
        return "unknown"
    rc, out = popen_capture([emu, "-accel-check"], timeout=30)
    out = out.strip()
    log("info", "加速检测: " + out.replace("\n", " | "))
    if "is installed and usable" in out or "WHPX" in out and "usable" in out:
        return "ok"
    if "HAXM" in out or "hypervisor" in out.lower():
        return "check"
    return "slow"
