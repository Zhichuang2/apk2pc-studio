"""命令行向导：双击就能用，不启浏览器、不占端口、不起 HTTP 服务。

设计参考 APK2EXE 的 Launcher：控制台输出每一步 + 出错暂停，绝不闪退。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from . import apk as apkmod
from . import config as C
from . import envpath
from . import pipeline, provision

MENU = """\
============================================================
  APK2PC Studio  {ver}     把 APK 变成能在电脑上跑的程序
============================================================
  1) 一键跑起来   解包 → Java 源码 → 重建签名 → 装进模拟器并打开
  2) 只拆包       解包 + 反编译出 Java（想看代码 / 想改东西选这个）
  3) 重新打包     我改过 src 里的 smali 或资源，重新打包并签名
  4) 环境检查     检查 / 补装缺失的工具与安卓运行时
  5) 生成启动器   把重建签名的 APK 做成「双击就能跑」的单个 EXE
  6) 输出位置     查看 / 切换成品路径（默认程序目录下的 output\\）
  7) 清理垃圾     删掉中间副本 / 旧构建残留，腾出磁盘
  0) 退出
------------------------------------------------------------"""


def say(text: str = "") -> None:
    print(text, flush=True)


def pause(msg: str = "按回车键退出…") -> None:
    say()
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def ask(prompt: str, default: str = "") -> str:
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0) from None
    # Windows 拖文件进控制台会带引号
    value = value.strip('"').strip("'").strip()
    return value or default


def ask_apk_path() -> Path | None:
    last = str(C.settings.get("last_apk") or "")
    tip = f"（直接回车用上次的：{last}）" if last else ""
    raw = ask(f"把 APK 文件拖进来，或粘贴完整路径 {tip}\n> ")
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        say(f"✘ 找不到这个文件：{p}")
        return None
    if p.suffix.lower() != ".apk":
        say(f"✘ 这不是 .apk 文件：{p.name}")
        return None
    C.settings["last_apk"] = str(p)
    C.settings.save()
    return p


def log(level: str, text: str) -> None:
    if level == "progress":
        sys.stdout.write("\r" + text[:110].ljust(112))
        sys.stdout.flush()
        return
    tag = {"ok": "  ✔", "warn": "  !", "error": "  ✘", "cmd": "  >"}.get(level, "   ")
    if level == "progress":
        return
    say(f"{tag} {text}")


def ensure_tools(action: str, logfn) -> None:
    """按动作装配，不一次性把 2 GB 模拟器全拉下来。"""
    provision.ensure_apktool(logfn)
    if action in ("full", "decode", "java"):
        try:
            provision.ensure_jadx(logfn)
        except Exception as e:  # noqa: BLE001
            logfn("warn", f"Java 反编译器不可用，将跳过源码：{e}")
    if action in ("full", "build", "launcher"):
        provision.ensure_buildtools(logfn)
    if action in ("full", "run"):
        root, need_install = provision.resolve_runtime(ask, logfn)
        if root is None:
            raise RuntimeError("没能确定安卓模拟器位置")
        if need_install:
            logfn("info", f"首次使用，运行时将安装到 {root}（所有成品共享）")
        provision.ensure_emulator_runtime(logfn)
    provision.sync_tool_path(logfn)


def run_job(action: str, params: dict) -> tuple[bool, dict]:
    """同步跑流水线，日志直接打到控制台（不经过 HTTP）。"""
    jm = pipeline.JobManager()
    job = jm.start(action, params)
    cursor = 0
    last_step = ""
    while True:
        snap = job.to_dict(cursor)
        for item in snap["logs"]:
            lvl, text = item.get("level", "info"), item.get("text", "")
            if lvl == "out" and text.startswith("["):
                continue
            if lvl == "step" or (text.startswith("▶")):
                say()
            log(lvl, text)
        cursor = snap["cursor"]
        if snap["status"] != "running":
            say()
            ok = snap["status"] == "ok"
            if ok:
                say(f"✔ 完成，用时 {snap['elapsed']}s")
            elif snap.get("error"):
                say(f"✘ 失败：{snap['error']}")
            else:
                say(f"· 结束（{snap['status']}）")
            return ok, snap.get("result", {})
        # 步骤切换时打印一行进度
        cur = "|".join(f"{s['key']}:{s['status']}" for s in snap.get("steps", []))
        if cur != last_step:
            last_step = cur
        time.sleep(0.35)


def show_result(res: dict) -> None:
    for key, label in (("decoded", "解包目录（可改 smali / res）"),
                       ("java", "Java 源码"),
                       ("signed", "重建并签名的 APK"),
                       ("artifact", "成品（交付用）")):
        if res.get(key):
            say(f"  {label}: {res[key]}")
    if res.get("out_dir"):
        say(f"  成品目录: {res['out_dir']}")
    if res.get("launcher"):
        say(f"  独立启动器: {res['launcher']}")


def action_full() -> None:
    apk = ask_apk_path()
    if not apk:
        return
    say("\n[1/2] 检查工具链（已装过的会直接复用，不会重复下载）…")
    ensure_tools("full", log)
    say("\n[2/2] 开始全流程…")
    ok, res = run_job("full", {"apk": str(apk)})
    show_result(res)


def action_decode() -> None:
    apk = ask_apk_path()
    if not apk:
        return
    say("\n检查工具链…")
    ensure_tools("decode", log)
    ok, res = run_job("decode", {"apk": str(apk)})
    show_result(res)
    if ok:
        say("  改完 src 里的东西后，选 3) 重新打包就能出新包。")


def action_rebuild() -> None:
    say("把上次解包出的 src 目录路径粘进来（直接回车用最近一次）")
    last = _last_workspace()
    raw = ask(f"> ", last)
    if not raw:
        return
    src = Path(raw)
    if not (src / "apktool.yml").exists():
        say(f"✘ 这不像 apktool 解包目录（缺 apktool.yml）：{src}")
        return
    ws = src.parent
    say("\n检查工具链…")
    ensure_tools("build", log)
    ok, res = run_job("build", {"decoded": str(src), "workspace": str(ws),
                                "apk": str(C.settings.get("last_apk") or "")})
    show_result(res)


def action_env() -> None:
    say("\n== 工具链状态 ==")
    snap = C.env_snapshot()
    for t in snap["tools"]:
        mark = "✔" if t["ok"] else "✘"
        say(f"  {mark} {t['name']:<22} {t['path'] or t['hint']}")
    dirs = envpath.tool_dirs()
    say(f"\n  用户 PATH 中已注册 {len(dirs)} 个共享工具目录")
    for d in dirs:
        say(f"    {d}")
    if not all(t["ok"] for t in snap["tools"] if t["required"]):
        if ask("\n有必需项缺失，现在补装？[Y/n] ", "Y").lower() == "y":
            provision.ensure_apktool(log)
            try:
                provision.ensure_jadx(log)
            except Exception as e:  # noqa: BLE001
                log("warn", f"jadx: {e}")
            provision.ensure_buildtools(log)
            provision.sync_tool_path(log)
            say("完成。")


def action_launcher() -> None:
    """产物内嵌的是「重建签名后的 APK」，不是把原包原样套回去。"""
    last = str(C.settings.get("last_signed") or "")
    say(f"成品输出目录：{C.output_dir()}")
    if last and Path(last).exists():
        say(f"上次重建签名的包：{last}")
        use_last = ask("用它来生成启动器？[Y/n]（n = 另选一个 APK）", "Y")
        if use_last.lower() != "n":
            target = Path(last)
        else:
            p = ask_apk_path()
            if not p:
                return
            target = p
    else:
        say("还没有重建签名过的包。先选 1) 或 3) 生成，或现在直接选一个 APK：")
        p = ask_apk_path()
        if not p:
            return
        target = p
    if not target.exists():
        say(f"✘ 找不到：{target}")
        return
    info = apkmod.badging_info(target) or {"label": target.stem}
    label = apkmod.sanitize(info.get("label") or target.stem)
    out = C.output_dir() / label
    say("\n生成中…（产物是单个 EXE，自带运行时，不需要 Python）")
    try:
        exe = pipeline.make_launcher(target, info, out)
        say(f"✔ 已生成：{exe}")
        say("  拷到任何 Windows 电脑双击即可；安卓运行时全局共享，只下载一次。")
    except Exception as e:  # noqa: BLE001
        say(f"✘ 生成失败：{e}")


def action_output() -> None:
    cur = C.output_dir()
    say(f"\n当前成品输出目录：{cur}")
    say("  默认 = 程序自身目录下的 output\\")
    say("  直接回车 = 恢复默认；输入 r = 保留不变；也可把文件夹拖进来。")
    raw = ask("> ", "r")
    if raw.strip().lower() == "r":
        return
    if not raw.strip():
        C.settings["output_dir"] = ""
    else:
        p = Path(raw.strip('"').strip())
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            say(f"✘ 这个目录用不了：{e}")
            return
        C.settings["output_dir"] = str(p.resolve())
    C.settings.save()
    say(f"✔ 已切换为：{C.output_dir()}")


def action_clean() -> None:
    """只删中间副本和构建残留，绝不碰成品目录 / src / java。"""
    import shutil
    from pathlib import Path

    protected = {str(C.output_dir().resolve()).lower()}
    freed = 0
    deleted = 0

    def unlink(p: Path) -> None:
        nonlocal freed, deleted
        try:
            if p.is_file() and p.stat().st_size >= 0:
                freed += p.stat().st_size
                p.unlink()
                deleted += 1
        except OSError:
            pass

    # 工作区里的未签名 / 已对齐中间包、idsig
    try:
        for ws in C.WORKSPACE.iterdir():
            if not ws.is_dir():
                continue
            for p in ws.glob("*.apk"):
                n = p.name.lower()
                if "-unsigned.apk" in n or "-aligned.apk" in n or n.endswith(".idsig"):
                    unlink(p)
            for p in ws.glob("*.idsig"):
                unlink(p)
    except OSError:
        pass

    # 构建残留（保留 dist 里最新的成品）
    for junk in ("build_launcher", "build_v2", "__pycache__"):
        d = C.program_dir() / junk
        if d.is_dir():
            try:
                shutil.rmtree(d, ignore_errors=True)
                deleted += 1
            except OSError:
                pass
    for spec in C.program_dir().glob("*.spec"):
        unlink(spec)

    say(f"✔ 已清理 {deleted} 项，释放约 {freed/1048576:.0f} MB")
    say(f"  成品目录未动：{C.output_dir()}")
    say("  提示：中间副本现在在签名成功后会自动删除，一般不用手动清。")


def _last_workspace() -> str:
    ws = C.WORKSPACE
    try:
        dirs = sorted((p for p in ws.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for d in dirs:
            if (d / "src" / "apktool.yml").exists():
                return str(d / "src")
    except OSError:
        pass
    return ""


def run_cli() -> int:
    say(MENU.format(ver=C.APP_VERSION))
    say("  提示：路径可以直接把文件拖进这个窗口；直接回车 = 选 1")
    while True:
        try:
            choice = ask("\n请选择 [1]: ", "1")
        except SystemExit:
            return 0
        try:
            if choice == "0":
                return 0
            if choice == "1":
                action_full()
            elif choice == "2":
                action_decode()
            elif choice == "3":
                action_rebuild()
            elif choice == "4":
                action_env()
            elif choice == "5":
                action_launcher()
            elif choice == "6":
                action_output()
            elif choice == "7":
                action_clean()
            else:
                say("输入 0-7 就行")
                continue
        except (KeyboardInterrupt, SystemExit):
            say("\n已取消")
        except Exception as e:  # noqa: BLE001
            import traceback
            say(f"✘ 出错了：{type(e).__name__}: {e}")
            traceback.print_exc()
        pause("按回车键回到菜单…")
        say(MENU.format(ver=C.APP_VERSION))
