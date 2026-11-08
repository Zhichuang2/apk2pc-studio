"""实验性轻量运行时（BETA）：静态可行性评估 + 产物清单。

边界，必须保持诚实：
- 本模块**不实现**轻量运行时。它只做静态判定，产出评估报告与一份供未来
  实现消费的清单（required_stubs）。
- 实际运行一律回落标准安卓运行时，成品行为不受影响。
- 零外部依赖：不调用 Java / apktool / adb / 模拟器，纯 Python 读 APK zip。
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from . import apk as apkmod
from . import config as C

SCHEMA = 1
SIZE_LIMIT_MB = 1024.0                 # 用户口径：产物超过 1 GB 就放弃这条路
TYPICAL_SHARED_RUNTIME_MB = 4096.0    # 未装运行时时的参考基准
MAX_DEX_BYTES = 96 * 1024 * 1024      # 只读前 96 MB dex，避免 2 GB 包卡死
MAX_DEX_FILES = 12
MAX_WALK_FILES = 8000

# DEX 内类名以 MUTF-8 的 ASCII 子串存在，字节级匹配即可
_MARKERS: dict[str, tuple[bytes, ...]] = {
    "gms": (b"Lcom/google/android/gms/", b"Lcom/google/firebase/",
            b"Lcom/google/android/play/", b"Lcom/android/vending/"),
    "play_integrity": (b"PlayIntegrity", b"playintegrity",
                       b"Lcom/google/android/play/core/integrity/"),
    "webview": (b"Landroid/webkit/", b"Landroidx/webkit/", b"org/chromium/"),
    "opengl": (b"Landroid/opengl/", b"GLES20", b"GLES30", b"libGLESv2"),
    "vulkan": (b"Vulkan", b"libvulkan", b"vkCreateInstance"),
    "renderscript": (b"Landroid/renderscript/", b"libRSSupport"),
    "reflection": (b"Ljava/lang/reflect/", b"Ljava/lang/invoke/", b"Class.forName"),
    "unity": (b"libunity", b"libil2cpp", b"com/unity3d/", b"UnityPlayer"),
    "unreal": (b"libUE4", b"libUnreal", b"com/epicgames/"),
    "flutter": (b"libflutter", b"io/flutter/", b"flutter_assets"),
    "react_native": (b"libreactnativejsi", b"com/facebook/react/", b"libhermes"),
    "cocos": (b"libcocos2d", b"org/cocos2dx/"),
    "packed": (b"libjiagu", b"libshell", b"libDexHelper", b"libprotect",
               b"com/qihoo/", b"com/stub/"),
}

# 文件名特征用 str；DEX 内容特征用 bytes，两者不能混用
_PACKED_NAMES = ("libjiagu", "libshell", "libdexhelper", "libprotect",
                 "libsecshell", "libtosprotection", "libmogosec")
_ENGINE_KEYS = ("unity", "unreal", "flutter", "react_native", "cocos")
_GRAPHIC_KEYS = ("opengl", "vulkan", "renderscript")

_BASE_STUBS = [
    "android.app.Activity", "android.app.Application", "android.content.Context",
    "android.content.Intent", "android.os.Bundle", "android.view.View",
    "android.view.ViewGroup", "android.widget.TextView", "android.widget.Button",
    "android.content.res.Resources", "android.content.SharedPreferences",
    "android.util.Log", "android.os.Handler", "android.os.Looper",
]


def enabled() -> bool:
    """BETA 开关：环境变量优先，其次用户设置，默认关。"""
    raw = os.environ.get("APK2PC_BETA_LIGHT", "").strip().lower()
    if raw in ("1", "true", "on", "yes"):
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    return bool(C.settings.get("beta_light_runtime", False))


def set_enabled(value: bool) -> None:
    C.settings["beta_light_runtime"] = bool(value)
    C.settings.save()


# ---------------------------------------------------------------- 采集

def _axml_strings(raw: bytes) -> str:
    """二进制 AndroidManifest 的字符串池：UTF-16LE / UTF-8 各解一次再拼起来。"""
    return (raw.decode("utf-16-le", errors="ignore") + "\n"
            + raw.decode("utf-8", errors="ignore"))


def _manifest_plain(decoded: Path) -> dict:
    """解包目录里的明文 manifest，比二进制字符串池准得多。"""
    out: dict = {"components": {}, "permissions": [], "features": [], "libraries": []}
    f = decoded / "AndroidManifest.xml"
    if not f.is_file():
        return out
    text = f.read_text(encoding="utf-8", errors="replace")
    for tag in ("activity", "service", "receiver", "provider"):
        out["components"][tag] = len(re.findall(rf"<{tag}\b", text))
    out["permissions"] = re.findall(
        r'<uses-permission[^>]*android:name="([^"]+)"', text)
    out["features"] = re.findall(
        r'<uses-feature[^>]*android:name="([^"]+)"', text)
    out["libraries"] = re.findall(
        r'<uses-library[^>]*android:name="([^"]+)"', text)
    return out


def _scan_zip(apk: Path) -> dict:
    """一遍遍历 zip 中央目录；只按需读少量 dex/manifest，不解压到磁盘。"""
    d: dict = {
        "entries": 0, "size": apk.stat().st_size,
        "dex_count": 0, "dex_mb": 0.0,
        "native_count": 0, "native_mb": 0.0, "abi": [], "so_names": [],
        "assets_mb": 0.0, "res_mb": 0.0, "arsc_mb": 0.0,
        "has_manifest": False, "has_arsc": False,
        "markers": {}, "axml": "", "hits": {},
    }
    blobs: list[bytes] = []
    dex_read = 0
    dex_files = 0
    so_names: list[str] = []
    abis: set[str] = set()

    with zipfile.ZipFile(apk) as z:
        for it in z.infolist():
            n = it.filename
            d["entries"] += 1
            size = it.file_size
            low = n.lower()

            if low.startswith("lib/") and n.count("/") >= 2:
                abis.add(n.split("/")[1])
                if low.endswith(".so"):
                    d["native_count"] += 1
                    d["native_mb"] += size / 1048576
                    so_names.append(Path(n).name)
            elif re.fullmatch(r"classes\d*\.dex", n):
                d["dex_count"] += 1
                d["dex_mb"] += size / 1048576
                if dex_files < MAX_DEX_FILES and dex_read + size <= MAX_DEX_BYTES:
                    try:
                        blobs.append(z.read(n))
                        dex_read += size
                        dex_files += 1
                    except (OSError, RuntimeError, zipfile.BadZipFile):
                        pass
            elif low.startswith("assets/"):
                d["assets_mb"] += size / 1048576
                if low.endswith((".so", ".dex", ".jar", ".js", ".json")) and size < 2 * 1048576:
                    blobs.append(z.read(n))
            elif low.startswith("res/"):
                d["res_mb"] += size / 1048576
            elif low == "resources.arsc":
                d["has_arsc"] = True
                d["arsc_mb"] += size / 1048576
            elif low == "androidmanifest.xml":
                d["has_manifest"] = True
                if size < 8 * 1048576:
                    try:
                        raw = z.read(n)
                        d["axml"] = _axml_strings(raw)
                        blobs.append(raw)
                    except (OSError, RuntimeError, zipfile.BadZipFile):
                        pass
            if any(p in low for p in _PACKED_NAMES):
                # 加固壳文件名也是强特征
                blobs.append(b"libshell libjiagu libDexHelper")

    # so 文件名本身是引擎/图形栈的强特征，一起参与匹配
    if so_names:
        blobs.append(" ".join(so_names).encode("utf-8", errors="ignore"))
    merged = b"\n".join(blobs)
    d["hits"] = {k: sum(merged.count(p) for p in pats) for k, pats in _MARKERS.items()}
    d["markers"] = {k: (v > 0) for k, v in d["hits"].items()}
    d["abi"] = sorted(abis)
    d["so_names"] = sorted(set(so_names))[:24]
    d["dex_mb"] = round(d["dex_mb"], 2)
    d["native_mb"] = round(d["native_mb"], 2)
    d["assets_mb"] = round(d["assets_mb"], 2)
    d["res_mb"] = round(d["res_mb"], 2)
    d["arsc_mb"] = round(d["arsc_mb"], 2)
    return d


def _runtime_footprint_mb() -> tuple[float, str]:
    """本机共享安卓运行时体积；未安装时用典型值兜底。"""
    total = 0
    counted = 0
    root = C.sdk_root()
    try:
        for base in (root / "system-images", root / "emulator"):
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if counted >= MAX_WALK_FILES:
                    break
                try:
                    if p.is_file():
                        total += p.stat().st_size
                        counted += 1
                except OSError:
                    pass
    except OSError:
        pass
    if total > 100 * 1048576:
        return round(total / 1048576, 1), "measured"
    return TYPICAL_SHARED_RUNTIME_MB, "typical"


def _required_stubs(d: dict) -> list[str]:
    """指示性清单：按命中的特征列出需要实现的 API 面（不保证完整）。"""
    stubs = list(_BASE_STUBS)
    if d["markers"].get("webview"):
        stubs += ["android.webkit.WebView", "android.webkit.WebViewClient"]
    if d["markers"].get("opengl"):
        stubs += ["android.opengl.GLSurfaceView", "javax.microedition.khronos.egl.EGL10"]
    if d["markers"].get("vulkan"):
        stubs += ["android.hardware.vulkan.Vulkan"]
    if d["markers"].get("gms"):
        stubs += ["com.google.android.gms.* (Binder 服务，非普通类)"]
    if d["markers"].get("reflection"):
        stubs += ["java.lang.reflect.* / Class.forName（静态分析无法穷尽）"]
    if d["native_count"]:
        stubs += ["android.system.* / bionic linker / JNI（Java stub 覆盖不到）"]
    return stubs


# ---------------------------------------------------------------- 判定

def evaluate(apk: Path, decoded: Path | None = None, info: dict | None = None) -> dict:
    """静态判定：能不能走「不加载完整安卓系统」的轻量路径。"""
    d = _scan_zip(apk)
    meta = dict(info or {})
    if decoded and decoded.is_dir():
        plain = _manifest_plain(decoded)
        try:
            meta.update({k: v for k, v in apkmod.read_apktool_yml(decoded).items()
                         if k in ("minSdk", "targetSdk", "versionCode", "versionName")})
        except OSError:
            pass
    else:
        plain = {"components": {}, "permissions": [], "features": [], "libraries": []}

    axml = d["axml"]
    permissions = sorted(set(plain["permissions"])
                         | set(re.findall(r"android\.permission\.[A-Z_]+", axml)))
    features = sorted(set(plain["features"])
                      | set(re.findall(r"android\.(?:hardware|software)\.[A-Za-z0-9_.]+", axml)))
    if any("opengl" in f.lower() or "vulkan" in f.lower() for f in features):
        d["markers"]["opengl"] = True

    blockers: list[dict] = []
    warnings: list[str] = []
    penalty = 0

    def block(code: str, text: str, cost: int) -> None:
        nonlocal penalty
        blockers.append({"code": code, "text": text})
        penalty += cost

    arm_only = bool(d["abi"]) and not any(a.startswith(("x86", "mips")) for a in d["abi"])
    if arm_only:
        block("ARM_NATIVE", f"native so 只有 {', '.join(d['abi'])}：需 ARM->x86 转译 + "
                            "bionic/linker，会把体积推回 G 级", 60)
    elif d["native_count"]:
        block("NATIVE_LIB", f"含 {d['native_count']} 个 native so：Java stub 覆盖不到 "
                            "Android linker / JNI", 35)
    if d["markers"].get("gms") or d["markers"].get("play_integrity"):
        block("GMS", "依赖 GMS / Play 服务 / Play Integrity：需伪造整套 Binder 服务与账号体系", 30)
    if any(d["markers"].get(k) for k in _GRAPHIC_KEYS) or \
       any(d["markers"].get(k) for k in _ENGINE_KEYS):
        block("GRAPHICS", "依赖游戏引擎 / OpenGL ES / Vulkan：图形栈需重写到 D3D", 30)
    if d["dex_count"] == 0 or d["markers"].get("packed"):
        block("PACKED", "未发现 classes*.dex 或疑似加固：静态翻译拿不到可编译输入", 40)
    if d["markers"].get("webview"):
        block("WEBVIEW", "依赖 WebView / Chromium：不是 Framework 类，是独立运行时", 15)
    if d["hits"].get("reflection", 0) > 300:
        block("REFLECTION", "反射调用密集：静态分析无法穷尽实际 API 调用集", 10)
    if d["dex_mb"] > 40 or d["dex_count"] > 8:
        block("LARGE_API_SURFACE", f"DEX {d['dex_mb']} MB / {d['dex_count']} 个："
                                   "需要覆盖的 Framework API 面过大", 10)

    if not blockers and d["hits"].get("reflection", 0):
        warnings.append("存在反射调用，裁剪后的 stub 集可能漏掉运行时才出现的 API")
    if len(permissions) > 20:
        warnings.append(f"申请了 {len(permissions)} 项权限，通常意味着较多系统服务依赖")
    if sum(plain["components"].values()) > 40:
        warnings.append("四大组件数量多，生命周期与 Binder 交互复杂")
    if not plain["components"] and not decoded:
        warnings.append("未使用解包目录，组件/权限来自二进制 manifest 字符串池，可能不完整")

    # 体积估算：只按数据量做经验估算，不含 Framework 实现成本
    asset_mb = round(
        12.0 + d["dex_mb"] * 0.40 + d["native_mb"] * 1.05
        + d["arsc_mb"] * 0.60 + d["assets_mb"] * 0.05 + d["res_mb"] * 0.10, 1)
    shared_mb, source = _runtime_footprint_mb()
    over_limit = asset_mb > SIZE_LIMIT_MB
    over_runtime = asset_mb > shared_mb
    if over_limit or over_runtime:
        block("SIZE_GUARD", f"估算产物 {asset_mb} MB 超过止损线"
                            f"（1 GB 阈值{'已触发' if over_limit else '未触发'} / "
                            f"共享运行时 {shared_mb} MB{'已触发' if over_runtime else '未触发'}），放弃轻量路径", 100)

    score = max(0, 100 - penalty)
    eligible = not blockers and score >= 70

    return {
        "schema": SCHEMA,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "apk": str(apk),
        "label": meta.get("label") or apk.stem,
        "package": meta.get("package") or "",
        "sdk": {"min": meta.get("minSdk", ""), "target": meta.get("targetSdk", "")},
        "size_mb": round(d["size"] / 1048576, 1),
        "entries": d["entries"],
        "dex": {"count": d["dex_count"], "mb": d["dex_mb"]},
        "abi": d["abi"],
        "native": {"count": d["native_count"], "mb": d["native_mb"],
                   "samples": d["so_names"], "arm_only": arm_only},
        "resources": {"assets_mb": d["assets_mb"], "res_mb": d["res_mb"],
                      "arsc_mb": d["arsc_mb"]},
        "features": {k: bool(d["markers"].get(k)) for k in _MARKERS},
        "feature_hits": d["hits"],
        "components": plain["components"],
        "permissions": permissions[:40],
        "uses_features": features[:40],
        "required_stubs": _required_stubs(d),
        "blockers": blockers,
        "warnings": warnings,
        "score": score,
        "verdict": "eligible" if eligible else "fallback",
        "verdict_text": ("通过静态判定：具备轻量路径的理论条件"
                         if eligible else "不通过静态判定：自动回落到标准安卓运行时"),
        "estimate": {
            "asset_mb": asset_mb,
            "shared_runtime_mb": shared_mb,
            "shared_runtime_source": source,
            "size_limit_mb": SIZE_LIMIT_MB,
            "over_size_limit": over_limit,
            "over_shared_runtime": over_runtime,
            "size_guard": "triggered" if (over_limit or over_runtime) else "clear",
            "per_apk_vs_shared": "轻量产物是每个 APK 各带一份；安卓运行时是全局共享、只下载一次",
            "disclaimer": "按数据量的经验估算，不是实测；不含 Framework / stub 实现成本",
        },
        "notes": [
            "本评估只做静态可行性判定，当前版本尚未实现轻量运行时。",
            "实际运行一律使用标准安卓运行时；本开关不会让成品失去可运行性。",
            "输出的 light-runtime.json 是供未来实现消费的指示性清单，不代表已注入任何层。",
        ],
    }


# ---------------------------------------------------------------- 产物

def render_markdown(r: dict) -> str:
    e, n, f = r["estimate"], r["native"], r["features"]
    lines = [
        f"# BETA 轻量运行时评估 · {r['label']}",
        "",
        f"- 生成时间：{r['generated']}",
        f"- APK：{r['apk']}（{r['size_mb']} MB，{r['entries']} 个条目）",
        f"- 包名：{r['package'] or '未识别'}　minSdk/targetSdk：{r['sdk']['min'] or '?'}/{r['sdk']['target'] or '?'}",
        f"- 结论：**{r['verdict_text']}**　评分：{r['score']}/100",
        "",
        "> 本评估只做静态可行性判定，当前版本尚未实现轻量运行时；实际运行一律回落标准安卓运行时。",
        "",
        "## 为什么",
        "",
    ]
    if r["blockers"]:
        for b in r["blockers"]:
            lines.append(f"- **{b['code']}**　{b['text']}")
    else:
        lines.append("- 未发现硬阻断项。")
    for w in r["warnings"]:
        lines.append(f"- 注意：{w}")
    lines += [
        "",
        "## 扫描明细",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| DEX | {r['dex']['count']} 个 / {r['dex']['mb']} MB |",
        f"| native so | {n['count']} 个 / {n['mb']} MB，ABI：{', '.join(r['abi']) or '无'} |",
        f"| 资源 | assets {r['resources']['assets_mb']} MB / res {r['resources']['res_mb']} MB / arsc {r['resources']['arsc_mb']} MB |",
        f"| 四大组件 | {r['components'] or '未取到（未提供解包目录）'} |",
        f"| 权限 | {len(r['permissions'])} 项 |",
        f"| 反射命中 | {r['feature_hits'].get('reflection', 0)} |",
        "",
        "命中的特征：" + (", ".join(k for k, v in f.items() if v) or "无"),
        "",
        "## 体积与止损线",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 估算轻量产物 | {e['asset_mb']} MB |",
        f"| 1 GB 阈值 | {e['size_limit_mb']:.0f} MB · {'已触发' if e['over_size_limit'] else '未触发'} |",
        f"| 本机共享安卓运行时 | {e['shared_runtime_mb']} MB（{e['shared_runtime_source']}）· {'已超过' if e['over_shared_runtime'] else '未超过'} |",
        f"| 止损线 | **{e['size_guard']}** |",
        "",
        f"- {e['per_apk_vs_shared']}。",
        f"- {e['disclaimer']}。",
        "",
        "## 供未来实现消费的指示性 stub 清单",
        "",
    ]
    lines += [f"- `{s}`" for s in r["required_stubs"]]
    lines += ["", "## 说明", ""] + [f"- {t}" for t in r["notes"]] + [""]
    return "\n".join(lines)


def write_report(report: dict, out_dir: Path, label: str) -> tuple[Path, Path]:
    """报告与清单一起落到成品目录（成果优先，不留在工作区）。"""
    d = out_dir / apkmod.sanitize(label)
    d.mkdir(parents=True, exist_ok=True)
    md = d / "BETA-轻量运行时评估.md"
    js = d / "light-runtime.json"
    md.write_text(render_markdown(report), encoding="utf-8")
    payload = {
        "schema": report["schema"], "generated": report["generated"],
        "apk": report["apk"], "label": report["label"], "package": report["package"],
        "verdict": report["verdict"], "verdict_text": report["verdict_text"],
        "score": report["score"], "abi": report["abi"],
        "features": report["features"], "blockers": report["blockers"],
        "required_stubs": report["required_stubs"], "estimate": report["estimate"],
        "note": "供未来轻量运行时消费；当前版本不实现，成品一律走标准安卓运行时",
        "notes": report["notes"],
    }
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js
