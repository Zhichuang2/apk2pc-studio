"""把 APK 直接「焊」进启动器 EXE 里（参考 APK2EXE 的思路：产物只含启动器 + APK）。

文件尾部布局（小端）：

    [ APK 原始字节 ][ config JSON(utf-8) ][ uint64 json 长度 ][ 8 字节 MAGIC ]

启动时只读最后 16 字节判断是不是自家产物，命中就按 json 里的 apk_len 倒推
APK 的偏移，把它解到 %LOCALAPPDATA%\\APK2PCStudio\\apps\\ 下再跑，
所以单个 EXE 就是「这个应用」本身，不需要旁边再放一个 apk 文件。
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

MAGIC = b"APK2EXE1"
TAIL = 16  # json 长度(8) + magic(8)


def build_embedded(exe_path: Path, apk_path: Path, cfg: dict) -> Path:
    """在 exe 副本尾部追加 APK + 配置。"""
    apk_bytes = apk_path.stat().st_size
    payload = dict(cfg)
    payload["apk_len"] = apk_bytes

    with open(exe_path, "ab") as f:
        with open(apk_path, "rb") as src:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        f.write(raw)
        f.write(struct.pack("<Q", len(raw)))
        f.write(MAGIC)
    return exe_path


def read_embedded(exe_path: Path) -> dict | None:
    """读取自身尾部内嵌配置；不是内嵌启动器就返回 None。"""
    try:
        size = exe_path.stat().st_size
        if size < TAIL + 32:
            return None
        with open(exe_path, "rb") as f:
            f.seek(-TAIL, 2)
            tail = f.read(TAIL)
            if tail[8:16] != MAGIC:
                return None
            json_len = struct.unpack("<Q", tail[:8])[0]
            if json_len <= 0 or json_len > size - TAIL:
                return None
            f.seek(-TAIL - json_len, 2)
            cfg = json.loads(f.read(json_len).decode("utf-8"))
        if "apk_len" not in cfg:
            return None
        cfg["_apk_offset"] = size - TAIL - json_len - int(cfg["apk_len"])
        cfg["_apk_len"] = int(cfg["apk_len"])
        cfg["_json_len"] = json_len
        return cfg
    except Exception:
        return None


def rewrite_config(exe_path: Path, patch: dict) -> bool:
    """只重写尾部那段 JSON，不动前面的 APK 字节。

    给 2 GB 级的启动器改个参数（比如关掉全屏）不该再复制一份 2 GB 文件。
    """
    cfg = read_embedded(exe_path)
    if not cfg:
        return False
    apk_len = int(cfg["_apk_len"])
    json_len = int(cfg["_json_len"])
    new = {k: v for k, v in cfg.items() if not k.startswith("_")}
    new.update(patch)
    new["apk_len"] = apk_len
    raw = json.dumps(new, ensure_ascii=False).encode("utf-8")
    cut = exe_path.stat().st_size - TAIL - json_len
    try:
        with open(exe_path, "r+b") as f:
            f.truncate(cut)
            f.seek(cut)
            f.write(raw)
            f.write(struct.pack("<Q", len(raw)))
            f.write(MAGIC)
        return True
    except Exception:
        return False


def extract_apk(exe_path: Path, cfg: dict, dest_dir: Path) -> Path | None:
    """把内嵌 APK 解到 dest_dir（按名字+大小缓存，避免每次重写）。"""
    name = cfg.get("file_name") or "app.apk"
    if not name.lower().endswith(".apk"):
        name += ".apk"
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / name
    want = int(cfg.get("_apk_len") or 0)
    if out.exists() and out.stat().st_size == want:
        return out
    try:
        with open(exe_path, "rb") as f:
            f.seek(int(cfg["_apk_offset"]))
            remaining = want
            with open(out, "wb") as w:
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    w.write(chunk)
                    remaining -= len(chunk)
        return out if out.stat().st_size == want else None
    except Exception:
        return None
