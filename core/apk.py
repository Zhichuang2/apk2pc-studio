"""APK / 反编译产物的信息提取（不依赖第三方库）。"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from . import config as C
from .runner import popen_capture

_PKG_RE = re.compile(r'package="([^"]+)"')
_LABEL_RE = re.compile(r'android:label="([^"]+)"')
_ACT_RE = re.compile(r'<activity\b[^>]*?android:name="([^"]+)"[^>]*>(.*?)</activity>', re.S)
_ACT_SELF_RE = re.compile(r'<activity\b[^>]*?android:name="([^"]+)"[^>]*/>', re.S)


def sanitize(name: str, fallback: str = "app") -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())
    s = re.sub(r"\s+", "_", s)
    return s[:64] or fallback


def read_apktool_yml(decoded: Path) -> dict:
    """apktool.yml 是 yaml，但结构极简，用行扫描足够，避免装 pyyaml。"""
    f = decoded / "apktool.yml"
    info: dict = {}
    if not f.exists():
        return info
    text = f.read_text(encoding="utf-8", errors="replace")
    for key, pat in (
        ("versionCode", r"versionCode:\s*'?([^'\n]+)'?"),
        ("versionName", r"versionName:\s*'?([^'\n]+)'?"),
        ("minSdk", r"minSdkVersion:\s*'?([^'\n]+)'?"),
        ("targetSdk", r"targetSdkVersion:\s*'?([^'\n]+)'?"),
        ("apkFileName", r"apkFileName:\s*'?([^'\n]+)'?"),
    ):
        m = re.search(pat, text)
        if m:
            info[key] = m.group(1).strip()
    return info


def resolve_string(res_dir: Path, ref: str) -> str:
    """把 @string/xxx 解析成真实文案。"""
    if not ref.startswith("@"):
        return ref
    name = ref.split("/", 1)[-1]
    for cand in (res_dir / "values" / "strings.xml",):
        if not cand.exists():
            continue
        txt = cand.read_text(encoding="utf-8", errors="replace")
        m = re.search(rf'<string name="{re.escape(name)}"[^>]*>(.*?)</string>', txt, re.S)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return ref


def manifest_info(decoded: Path) -> dict:
    """从 apktool 解包后的明文 AndroidManifest 提取关键信息。"""
    out: dict = {"package": "", "label": "", "main_activity": "", "permissions": []}
    mf = decoded / "AndroidManifest.xml"
    if not mf.exists():
        return out
    text = mf.read_text(encoding="utf-8", errors="replace")

    m = _PKG_RE.search(text)
    if m:
        out["package"] = m.group(1)

    app_block = text
    am = re.search(r"<application\b(.*?)>", text, re.S)
    if am:
        app_block = am.group(1)
    lm = _LABEL_RE.search(app_block)
    if lm:
        out["label"] = resolve_string(decoded / "res", lm.group(1))

    # 带子节点的 activity
    for act, body in _ACT_RE.findall(text):
        if "android.intent.action.MAIN" in body and "LAUNCHER" in body:
            out["main_activity"] = act
            break
    if not out["main_activity"]:
        # 自闭合写法没法带 intent-filter，退化为第一个 activity
        for act in _ACT_SELF_RE.findall(text):
            out["main_activity"] = act
            break
    if out["main_activity"] and out["main_activity"].startswith("."):
        out["main_activity"] = out["package"] + out["main_activity"]
    elif out["main_activity"] and "." not in out["main_activity"]:
        out["main_activity"] = f"{out['package']}.{out['main_activity']}"

    out["permissions"] = re.findall(r'<uses-permission[^>]*android:name="([^"]+)"', text)

    meta = read_apktool_yml(decoded)
    out.update({k: v for k, v in meta.items() if k in ("versionCode", "versionName", "minSdk", "targetSdk")})
    if not out.get("label"):
        out["label"] = out["package"].split(".")[-1] or "app"
    return out


def manifest_package_fallback(apk: Path) -> str:
    """aapt2 不可用时，从二进制 AndroidManifest.xml 的字符串池里兜底取包名。

    AXML 的字符串多以 UTF-16LE 存放，直接解出所有点分标识符再挑最像包名的那个；
    只用于极端场景（工具链没装全），正常路径仍走 aapt2。
    """
    try:
        with zipfile.ZipFile(apk) as z:
            raw = z.read("AndroidManifest.xml")
    except Exception:
        return ""
    text = raw.decode("utf-16-le", errors="ignore")
    cands = re.findall(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,}", text)
    skip = ("android.", "com.google.", "java.", "dalvik.", "org.w3c.", "org.xml.")
    for c in cands:
        if not c.startswith(skip) and not c.endswith((".xml", ".dex", ".so", ".png")):
            return c
    return ""


def badging_info(apk: Path) -> dict:
    """没有解包目录时，用 aapt2 dump badging 兜底。"""
    aapt = C.aapt2_exe()
    out: dict = {"package": "", "label": "", "main_activity": ""}
    if not aapt:
        out["package"] = manifest_package_fallback(apk)
        return out
    rc, txt = popen_capture([aapt, "dump", "badging", str(apk)], timeout=60)
    if rc != 0:
        return out
    m = re.search(r"package: name='([^']+)'", txt)
    if m:
        out["package"] = m.group(1)
    m = re.search(r"application-label:'([^']*)'", txt)
    if m:
        out["label"] = m.group(1)
    m = re.search(r"launchable-activity: name='([^']+)'", txt)
    if m:
        out["main_activity"] = m.group(1)
    return out


def zip_peek(apk: Path) -> dict:
    """不解包先看结构：是否加固（找不到 classes.dex 就是常见壳特征）。"""
    info = {"entries": 0, "dex": 0, "has_manifest": False, "libs": [], "size": 0, "packed_hint": ""}
    if not apk.exists():
        return info
    info["size"] = apk.stat().st_size
    try:
        with zipfile.ZipFile(apk) as z:
            names = z.namelist()
            info["entries"] = len(names)
            info["dex"] = sum(1 for n in names if re.fullmatch(r"classes\d*\.dex", n))
            info["has_manifest"] = "AndroidManifest.xml" in names
            info["libs"] = sorted({n.split("/")[1] for n in names
                                   if n.startswith("lib/") and n.count("/") >= 2})
            if not info["dex"]:
                suspect = [n for n in names if re.search(r"(stub|jiagu|shell|protect|dex2oat|libjiagu|libshell)", n, re.I)]
                info["packed_hint"] = ("疑似加固/壳: " + ", ".join(suspect[:3])) if suspect else \
                    "未发现 classes*.dex，APK 可能被加固，反编译结果会为空"
    except Exception as e:  # noqa: BLE001
        info["packed_hint"] = f"读取失败: {e}"
    return info


def count_sources(root: Path) -> dict:
    smali = len(list(root.rglob("*.smali"))) if root.exists() else 0
    java = len(list(root.rglob("*.java"))) if root.exists() else 0
    return {"smali": smali, "java": java}
