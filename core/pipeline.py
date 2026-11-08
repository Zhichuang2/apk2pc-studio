"""流水线：解包 → Java 源码 → 重建 → 对齐签名 → 安装运行 → 导出。"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import apk as apkmod
from . import config as C
from . import lightmode
from . import provision
from . import runtime
from .runner import LogFn, popen_capture, run, win_bat


class PipelineError(RuntimeError):
    pass


# ---------------------------------------------------------------- Job

class Job:
    """一次流水线执行，UI 靠 /api/job 轮询它。"""

    def __init__(self, kind: str, title: str, steps: list[tuple[str, str]], params: dict):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.params = params
        self.status = "running"          # running | ok | error | cancelled
        self.steps = [{"key": k, "name": n, "status": "pending", "detail": ""} for k, n in steps]
        self.logs: list[dict] = []
        self.result: dict = {}
        self.error = ""
        self.started = time.time()
        self.ended: float | None = None
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._seq = 0

    # -- 日志
    def log(self, level: str, text: str) -> None:
        with self._lock:
            self._seq += 1
            self.logs.append({"i": self._seq, "t": round(time.time() - self.started, 2),
                              "level": level, "text": str(text)[:4000]})

    # -- 步骤
    def _step(self, key: str) -> dict | None:
        for s in self.steps:
            if s["key"] == key:
                return s
        return None

    def begin(self, key: str, detail: str = "") -> None:
        s = self._step(key)
        if s:
            s["status"] = "running"
            s["detail"] = detail

    def ok_step(self, key: str, detail: str = "") -> None:
        s = self._step(key)
        if s:
            s["status"] = "ok"
            s["detail"] = detail

    def skip(self, key: str, detail: str = "") -> None:
        s = self._step(key)
        if s:
            s["status"] = "skip"
            s["detail"] = detail

    def fail_step(self, key: str, detail: str = "") -> None:
        s = self._step(key)
        if s:
            s["status"] = "error"
            s["detail"] = detail

    @property
    def progress(self) -> dict:
        done = sum(1 for s in self.steps if s["status"] in ("ok", "skip", "error"))
        return {"done": done, "total": len(self.steps),
                "pct": round(done * 100 / max(1, len(self.steps)))}

    def cancel(self) -> None:
        self.cancel_event.set()
        self.log("warn", "收到取消指令…")

    def to_dict(self, since: int = 0) -> dict:
        with self._lock:
            logs = [l for l in self.logs if l["i"] > since]
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "steps": self.steps, "progress": self.progress, "logs": logs,
            "cursor": self._seq, "result": self.result, "error": self.error,
            "elapsed": round((self.ended or time.time()) - self.started, 1),
            "params": self.params,
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self._lock = threading.Lock()

    def start(self, kind: str, params: dict) -> Job:
        apk_path = Path(params.get("apk", ""))
        steps = self._steps_for(kind)
        job = Job(kind, self._title_for(kind, apk_path), steps, params)
        with self._lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            for old in self.order[:-30]:
                self.jobs.pop(old, None)
            self.order = self.order[-30:]
        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        t.start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    @staticmethod
    def _steps_for(kind: str) -> list[tuple[str, str]]:
        base = [
            ("prepare", "准备工作区"),
            ("decode", "apktool 解包（资源 + smali）"),
            ("java", "反编译为 Java 源码"),
            ("build", "apktool 重建 APK"),
            ("sign", "zipalign + 签名"),
            ("run", "安装并运行在电脑上"),
        ]
        if kind == "light":
            return [("scan", "静态扫描 APK 依赖"), ("report", "生成 BETA 评估报告")]
        if kind == "full":
            return base
        if kind == "decode":
            return base[:3]
        if kind == "java":
            return [base[0], base[2]]
        if kind == "build":
            return [base[3], base[4]]
        if kind == "run":
            return [base[5]]
        if kind == "provision":
            return [("dl", "下载并装配组件")]
        return base

    @staticmethod
    def _title_for(kind: str, apk_path: Path) -> str:
        name = apk_path.stem or "APK"
        return {
            "full": f"全流程 · {name}",
            "decode": f"解包 · {name}",
            "java": f"Java 反编译 · {name}",
            "build": f"重建签名 · {name}",
            "run": f"运行 · {name}",
            "light": f"BETA 轻量评估 · {name}",
            "provision": "装配工具链 / 运行时",
        }.get(kind, name)

    # -- 执行
    def _run(self, job: Job) -> None:
        try:
            log = job.log
            if job.kind == "provision":
                target = job.params.get("target", "emulator")
                job.begin("dl")
                log("info", f"装配目标: {target}")
                if target == "jadx":
                    provision.ensure_java_decompiler(log)
                elif target == "buildtools":
                    provision.ensure_buildtools(log)
                elif target == "apktool":
                    provision.ensure_apktool(log)
                else:
                    provision.ensure_emulator_runtime(log)
                    provision.accel_check(log)
                job.ok_step("dl")
            else:
                runner = {
                    "decode": run_decode_only,
                    "java": run_java_only,
                    "build": run_build_only,
                    "run": run_only,
                    "light": run_light_only,
                    "full": run_full,
                }[job.kind]
                runner(job)
            job.status = "ok"
            log("ok", "✔ 全部完成")
        except PipelineError as e:
            job.status = "error"
            job.error = str(e)
            job.log("error", "✘ " + str(e))
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(e).__name__}: {e}"
            job.log("error", "✘ 未预期错误: " + job.error)
        finally:
            for s in job.steps:
                if s["status"] == "running":
                    s["status"] = "error"
            job.ended = time.time()


# ---------------------------------------------------------------- 路径规划

def workspace_for(apk_path: Path, info: dict) -> Path:
    label = apkmod.sanitize(info.get("label") or apk_path.stem)
    stamp = time.strftime("%m%d-%H%M%S")
    root = C.WORKSPACE / f"{label}_{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _check_cancel(job: Job) -> None:
    if job.cancel_event.is_set():
        raise PipelineError("用户已取消")


# ---------------------------------------------------------------- 各阶段

def step_prepare(job: Job, apk_path: Path) -> Path:
    job.begin("prepare")
    if not apk_path.exists() or apk_path.suffix.lower() != ".apk":
        raise PipelineError(f"不是有效的 APK 文件: {apk_path}")
    peek = apkmod.zip_peek(apk_path)
    job.log("info", f"APK 大小 {peek['size']/1048576:.1f} MB，{peek['entries']} 个条目，"
                    f"{peek['dex']} 个 dex")
    if peek["packed_hint"]:
        job.log("warn", peek["packed_hint"])

    badging = apkmod.badging_info(apk_path)
    root = workspace_for(apk_path, badging)
    job.log("ok", f"工作区: {root}")
    job.result["workspace"] = str(root)
    job.result["apk"] = str(apk_path)
    job.result["peek"] = peek
    job.ok_step("prepare", str(root))
    return root


def step_decode(job: Job, apk_path: Path, root: Path) -> Path:
    job.begin("decode")
    jar = provision.ensure_apktool(job.log)
    java = C.java_exe()
    if not java:
        raise PipelineError("缺少 Java 运行时")
    out = root / "src"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    # 注意：apktool 3.x 已移除 --use-aapt2（内部固定 aapt2）
    args = [str(java), "-jar", str(jar), "d", "-f"]
    if C.settings.get("extra_apktool_args"):
        args += C.settings["extra_apktool_args"].split()
    args += ["-o", str(out), str(apk_path)]
    rc = run(args, job.log, cancel=job.cancel_event, timeout=1800)
    if rc != 0 or not (out / "AndroidManifest.xml").exists():
        raise PipelineError("apktool 解包失败（加固壳/资源异常时会这样，日志里有原因）")

    info = apkmod.manifest_info(out)
    counts = apkmod.count_sources(out)
    job.result["decoded"] = str(out)
    job.result["info"] = info
    job.result["counts"] = counts
    job.log("info", f"包名 {info.get('package')} · {info.get('label')} · "
                    f"minSdk {info.get('minSdk','?')} targetSdk {info.get('targetSdk','?')}")
    job.log("info", f"smali 文件 {counts['smali']} 个，启动 Activity: "
                    f"{info.get('main_activity') or '未识别'}")
    if info.get("main_activity"):
        job.result["main_activity"] = info["main_activity"]
    job.ok_step("decode", f"{counts['smali']} 个 smali")
    return out


def step_java(job: Job, apk_path: Path, root: Path) -> Path | None:
    job.begin("java")
    try:
        kind, base = provision.ensure_java_decompiler(job.log)
    except Exception as e:  # noqa: BLE001
        job.log("warn", f"反编译器不可用（{e}），跳过 Java 源码，仅保留 smali")
        job.skip("java", "无可用反编译器")
        return None
    out = root / "java"
    shutil.rmtree(out, ignore_errors=True)
    if kind == "jadx":
        args = base + ["-d", str(out), "--no-res", "-j",
                       str(C.settings.get("jadx_threads", 4)), "--show-bad-code", str(apk_path)]
    else:
        args = base + ["decompile", "-o", str(out), str(apk_path)]
    job.log("info", f"使用 {kind} 反编译 → {out.name}")
    rc = run(args, job.log, cancel=job.cancel_event, timeout=7200)
    counts = apkmod.count_sources(out)
    if counts["java"] == 0 and kind == "jadx":
        ag = C.androguard_cmd()
        if ag:
            job.log("warn", "jadx 无产出，改用 androguard（DAD）再试一次")
            shutil.rmtree(out, ignore_errors=True)
            rc = run(ag + ["decompile", "-o", str(out), str(apk_path)],
                     job.log, cancel=job.cancel_event, timeout=7200)
            counts = apkmod.count_sources(out)
            kind = "androguard"
    if counts["java"] == 0:
        job.log("warn", f"{kind} 未产出 .java（rc={rc}；加固/多 dex 载荷时常见）")
        job.skip("java", "无产出")
        return None
    job.result["java"] = str(out)
    job.result["decompiler"] = kind
    job.result["counts"] = {**job.result.get("counts", {}), "java": counts["java"]}
    job.log("ok", f"Java 源文件 {counts['java']} 个 → {out}")
    job.ok_step("java", f"{counts['java']} 个 .java ({kind})")
    return out


def step_build(job: Job, root: Path) -> Path:
    job.begin("build")
    src = Path(job.result.get("decoded", root / "src"))
    if not (src / "AndroidManifest.xml").exists():
        raise PipelineError("没有可用的解包目录，请先执行解包")
    jar = provision.ensure_apktool(job.log)
    java = C.java_exe()
    unsigned = root / f"{src.name}-unsigned.apk"
    args = [str(java), "-jar", str(jar), "b", "-f"]
    aapt2 = C.aapt2_exe()
    if aapt2:
        args += ["--aapt", str(aapt2)]
    args += ["-o", str(unsigned), str(src)]
    rc = run(args, job.log, cancel=job.cancel_event, timeout=1800)
    if rc != 0 or not unsigned.exists():
        raise PipelineError("apktool 重建失败，检查 smali / 资源是否被改坏")
    job.result["unsigned"] = str(unsigned)
    job.log("ok", f"重建完成（未签名）: {unsigned.name} "
                  f"{unsigned.stat().st_size/1048576:.1f} MB")
    job.ok_step("build", unsigned.name)
    return unsigned


def ensure_keystore(job: Job) -> Path:
    if C.KEYSTORE.exists():
        return C.KEYSTORE
    kt = C.keytool_exe()
    if not kt:
        raise PipelineError("缺少 keytool（JDK 自带），无法生成签名密钥")
    job.log("info", "生成调试签名密钥（APK2PC debug keystore）")
    rc = run([str(kt), "-genkeypair", "-keystore", str(C.KEYSTORE), "-storetype", "PKCS12",
              "-alias", C.KEYSTORE_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
              "-validity", "10000", "-storepass", C.KEYSTORE_PASS, "-keypass", C.KEYSTORE_PASS,
              "-dname", "CN=APK2PC Studio, O=APK2PC, C=CN"],
             job.log, cancel=job.cancel_event, timeout=180)
    if rc != 0 or not C.KEYSTORE.exists():
        raise PipelineError("生成签名密钥失败")
    return C.KEYSTORE


def step_sign(job: Job, unsigned: Path, root: Path) -> Path:
    job.begin("sign")
    try:
        provision.ensure_buildtools(job.log)
    except Exception as e:  # noqa: BLE001
        raise PipelineError(f"签名工具不可用: {e}") from e
    zipalign = C.zipalign_exe()
    apksigner = C.apksigner_exe()
    if not zipalign or not apksigner:
        raise PipelineError("zipalign / apksigner 缺失")

    aligned = root / f"{unsigned.stem}-aligned.apk"
    signed = root / f"{unsigned.stem}-signed.apk"
    rc = run([str(zipalign), "-p", "-f", "4", str(unsigned), str(aligned)],
             job.log, cancel=job.cancel_event, timeout=600)
    if rc != 0:
        job.log("warn", "zipalign 失败（可能已对齐），继续签名")
        aligned = unsigned

    ks = ensure_keystore(job)
    rc = run(win_bat(apksigner, [
        "sign", "--ks", str(ks), "--ks-key-alias", C.KEYSTORE_ALIAS,
        "--ks-pass", f"pass:{C.KEYSTORE_PASS}", "--key-pass", f"pass:{C.KEYSTORE_PASS}",
        "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
        "--v3-signing-enabled", "true", "--out", str(signed), str(aligned),
    ]), job.log, cancel=job.cancel_event, timeout=900, env=C.java_env())
    if rc != 0 or not signed.exists():
        raise PipelineError("apksigner 签名失败")

    run(win_bat(apksigner, ["verify", "--print-certs", str(signed)]),
        job.log, cancel=job.cancel_event, timeout=300, env=C.java_env())
    job.result["signed"] = str(signed)
    job.result["out_dir"] = str(root)
    job.log("ok", f"已签名: {signed}")

    # 全流程会产出 unsigned / aligned / signed 三份同尺寸大包（2 GB 级时占 7 GB+）。
    # 签名成品才是交付物，中间副本立刻清掉，避免每个成品都白占一份磁盘。
    if not C.settings.get("keep_intermediates"):
        freed = 0
        for p in (unsigned, aligned):
            try:
                if p and p != signed and p.is_file():
                    freed += p.stat().st_size
                    p.unlink()
            except OSError:
                pass
        if freed:
            job.log("info", f"已清理中间副本，释放 {freed/1048576:.0f} MB（只保留签名成品）")
        idsig = Path(str(signed) + ".idsig")
        try:
            if idsig.is_file():
                idsig.unlink()
        except OSError:
            pass
    job.ok_step("sign", signed.name)
    return signed


def step_run(job: Job, apk_file: Path, info: dict) -> None:
    job.begin("run")
    verdict = job.result.get("light_verdict")
    if lightmode.enabled() and verdict:
        if verdict == "fallback":
            job.log("warn", "BETA：依赖判定不通过 → 已自动回落到标准安卓运行时（成品不受影响）")
        else:
            job.log("info", "BETA：静态判定通过，轻量清单已产出；当前版本仍回落标准运行时运行")
    if not C.adb_exe():
        raise PipelineError("缺少 adb")
    serial = runtime.pick_serial(job.params.get("serial"))
    if not serial and C.settings.get("auto_start_emulator"):
        provision.ensure_emulator_runtime(job.log)
        accel = provision.accel_check(job.log)
        if accel != "ok":
            job.log("warn", "硬件加速未就绪 → 模拟器可能起不来。"
                            "请到「启用或关闭 Windows 功能」勾选「Windows 虚拟机监控程序平台」(WHPX) 后重启")

        want_fs = bool(job.params.get("fullscreen"))
        quick = bool(C.settings.get("quick_boot", True))
        started = runtime.start_emulator(job.log, fullscreen=want_fs, cold=not quick)
        # 全屏在部分 Windows + swiftshader 组合上会秒退，直接退窗口模式重试一次
        if not started and want_fs:
            job.log("warn", "全屏模式启动失败，改用窗口模式重试")
            want_fs = False
            started = runtime.start_emulator(job.log, fullscreen=False, cold=not quick)

        if started:
            # 走快照时正常 10s 级；90s 还没起来说明快照不可用 → 冷启重试
            serial = runtime.wait_boot(job.log, timeout=90 if quick else 420)

        if not serial and quick:
            job.log("warn", "快照恢复超时，改用冷启动重试（首次约 1–2 分钟）")
            runtime.stop_all(job.log)
            if runtime.start_emulator(job.log, fullscreen=False, cold=True):
                serial = runtime.wait_boot(job.log, timeout=420)

        if not serial:
            tail = runtime.log_tail(runtime.last_emulator_log(), 12)
            if tail:
                job.log("info", "—— 模拟器最后输出 ——")
                for line in tail.splitlines():
                    job.log("out", line)
            job.log("warn", "开机超时/秒退：先看上面的模拟器输出；"
                            "常见原因是全屏模式、镜像损坏（可删 AVD 重建）、或 WHPX 未启用")
    elif not serial:
        raise PipelineError("没有可用设备，且未开启自动启动模拟器")
    if not serial:
        raise PipelineError("模拟器未能就绪")

    detail = runtime.device_detail(serial)
    job.log("info", f"设备 {detail['serial']} · {detail['model']} · Android {detail['android']}")

    pkg = info.get("package", "")
    act = info.get("main_activity", "")
    if not runtime.install(apk_file, serial, job.log, pkg):
        raise PipelineError("安装失败")
    if pkg:
        runtime.launch(pkg, act, serial, job.log)
    job.result["serial"] = serial
    job.result["package"] = pkg
    job.ok_step("run", serial)


# ---------------------------------------------------------------- 组合流程

def run_light_only(job: Job) -> None:
    """BETA 独立入口：纯静态评估，不调用 Java / apktool / adb / 模拟器。"""
    apk_path = Path(job.params["apk"])
    if not apk_path.is_file():
        raise PipelineError(f"找不到 APK：{apk_path}")
    raw = job.params.get("decoded") or ""
    decoded = Path(raw) if raw and Path(raw).is_dir() else None

    job.begin("scan")
    report = lightmode.evaluate(apk_path, decoded, job.params.get("info"))
    job.result["apk"] = str(apk_path)
    job.result["light_verdict"] = report["verdict"]
    job.result["light"] = {
        "verdict": report["verdict"], "verdict_text": report["verdict_text"],
        "score": report["score"], "blockers": report["blockers"],
        "estimate": report["estimate"], "abi": report["abi"],
    }
    job.log("info", f"DEX {report['dex']['count']} 个 / {report['dex']['mb']} MB · "
                    f"native so {report['native']['count']} 个 · "
                    f"ABI {', '.join(report['abi']) or '无'}")
    job.ok_step("scan", f"{report['entries']} 个条目，评分 {report['score']}/100")

    job.begin("report")
    md, js = lightmode.write_report(report, C.output_dir(), report["label"])
    job.result["report"] = str(md)
    job.result["report_json"] = str(js)
    job.result["out_dir"] = str(md.parent)
    job.log("ok", f"结论：{report['verdict_text']}")
    for b in report["blockers"]:
        job.log("warn", f"  {b['code']}：{b['text']}")
    e = report["estimate"]
    job.log("info", f"估算产物 {e['asset_mb']} MB · 1 GB 阈值"
                    f"{'已触发' if e['over_size_limit'] else '未触发'} · "
                    f"共享运行时 {e['shared_runtime_mb']} MB（{e['shared_runtime_source']}）")
    job.log("ok", f"报告：{md}")
    job.log("info", "当前版本尚未实现轻量运行时；实际运行一律回落标准安卓运行时。")
    job.ok_step("report", md.name)


def attach_light_report(job: Job, apk_path: Path, decoded: Path | None) -> None:
    """全流程附带评估：开关关掉 / 评估失败都不影响主流程。"""
    if not lightmode.enabled():
        return
    try:
        report = lightmode.evaluate(apk_path, decoded, job.result.get("info"))
        md, js = lightmode.write_report(report, C.output_dir(), report["label"])
        job.result["light_verdict"] = report["verdict"]
        job.result["report"] = str(md)
        job.result["report_json"] = str(js)
        job.log("info", f"BETA 轻量运行时评估：{report['verdict_text']}"
                        f"（评分 {report['score']}/100）")
        if report["verdict"] == "fallback":
            reasons = "; ".join(b["code"] for b in report["blockers"][:4]) or "依赖不满足"
            job.log("warn", f"BETA：依赖判定不通过（{reasons}），"
                            "运行阶段将自动回落到标准安卓运行时")
        job.log("ok", f"BETA 报告：{md}")
    except Exception as e:  # noqa: BLE001
        job.log("warn", f"BETA 评估跳过（不影响主流程）：{type(e).__name__}: {e}")


def run_decode_only(job: Job) -> None:
    apk_path = Path(job.params["apk"])
    root = step_prepare(job, apk_path)
    out = step_decode(job, apk_path, root)
    attach_light_report(job, apk_path, out)
    _check_cancel(job)
    step_java(job, apk_path, root)


def run_java_only(job: Job) -> None:
    apk_path = Path(job.params["apk"])
    root = step_prepare(job, apk_path)
    step_java(job, apk_path, root)


def run_build_only(job: Job) -> None:
    decoded = job.params.get("decoded") or job.params.get("src")
    root = Path(job.params.get("workspace") or C.WORKSPACE)
    if not decoded:
        raise PipelineError("重建需要先解包（或指定解包目录）")
    job.result["decoded"] = decoded
    job.result["workspace"] = str(root)
    job.skip("prepare", "使用已有解包目录")
    job.skip("java", "跳过")
    unsigned = step_build(job, root)
    _check_cancel(job)
    signed = step_sign(job, unsigned, root)
    info = job.result.get("info") or apkmod.badging_info(Path(job.params["apk"])) or {}
    publish_artifact(job, signed, info)


def run_only(job: Job) -> None:
    apk_path = Path(job.params["apk"])
    info = job.params.get("info") or apkmod.badging_info(apk_path)
    if not info.get("package"):
        # 现场解包拿信息代价高，用 apktool.yml 兜底：先试 aapt2，再退回空
        info = {**info, "package": "", "main_activity": ""}
    for k in ("prepare", "decode", "java", "build", "sign"):
        job.skip(k, "跳过")
    job.result["apk"] = str(apk_path)
    step_run(job, apk_path, info)


def run_full(job: Job) -> None:
    apk_path = Path(job.params["apk"])
    root = step_prepare(job, apk_path)
    _check_cancel(job)
    out = step_decode(job, apk_path, root)
    attach_light_report(job, apk_path, out)
    _check_cancel(job)
    step_java(job, apk_path, root)
    _check_cancel(job)
    unsigned = step_build(job, root)
    _check_cancel(job)
    signed = step_sign(job, unsigned, root)
    _check_cancel(job)
    info = job.result.get("info", {})
    publish_artifact(job, signed, info)
    step_run(job, signed, info)


def publish_artifact(job: Job, signed: Path, info: dict) -> Path:
    """把签名成品发布到用户指定的输出目录（默认程序自身目录下 output\\）。

    工作区只留 src/ 与 java/ 供再次修改重打包；交付物统一放输出目录，
    这样「程序是辅助、产物才是成果」——换台机器只需要拷这个目录。
    """
    label = apkmod.sanitize(info.get("label") or signed.stem)
    out = C.output_dir() / label
    out.mkdir(parents=True, exist_ok=True)
    dest = out / f"{label}-signed.apk"
    try:
        if signed.resolve() != dest.resolve():
            shutil.copy2(signed, dest)
        job.result["artifact"] = str(dest)
        job.result["out_dir"] = str(out)
        C.settings["last_signed"] = str(dest)
        C.settings["last_workspace"] = str(job.result.get("workspace") or "")
        C.settings.save()
        job.log("ok", f"成品已输出到 {dest}")
    except OSError as e:
        job.log("warn", f"复制到输出目录失败（成品仍在工作区）: {e}")
    return dest


# ---------------------------------------------------------------- 导出

def export_gradle(workspace: Path, decoded: Path, java_dir: Path | None, info: dict) -> Path:
    """把反编译结果拼成一个 Android Studio 可打开的工程骨架。

    诚实提示：jadx 的 Java 几乎不可能 100% 直编（混淆、内联、资源 id、native 依赖），
    这里给的是「二次开发 / 阅读」起点，不是可一条命令 build 成功的工程。
    """
    pkg = info.get("package") or "com.example.app"
    ns = pkg
    out = workspace / "gradle-project"
    # 模块叫 :app，AGP 只认 <工程>/app/src/main —— 少一层 app 目录，
    # Android Studio 打开后会显示「零个源文件」，这是最容易踩的坑。
    module = out / "app"
    main = module / "src" / "main"
    (main / "java").mkdir(parents=True, exist_ok=True)

    if java_dir and java_dir.exists():
        for p in java_dir.rglob("*.java"):
            rel = p.relative_to(java_dir)
            dst = main / "java" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)

    if (decoded / "res").exists():
        shutil.copytree(decoded / "res", main / "res", dirs_exist_ok=True)
    src_manifest = decoded / "AndroidManifest.xml"
    if src_manifest.exists():
        # AGP 8 不允许 manifest 里再出现 package="..."（改名到 build.gradle 的 namespace）
        txt = src_manifest.read_text(encoding="utf-8", errors="replace")
        txt = re.sub(r'\spackage="[^"]*"', "", txt, count=1)
        (main / "AndroidManifest.xml").write_text(txt, encoding="utf-8")
    if (decoded / "assets").exists():
        shutil.copytree(decoded / "assets", main / "assets", dirs_exist_ok=True)
    if (decoded / "lib").exists():
        shutil.copytree(decoded / "lib", main / "jniLibs", dirs_exist_ok=True)

    min_sdk = str(info.get("minSdk") or "21").strip("'")
    target_sdk = str(info.get("targetSdk") or "34").strip("'")
    ver_name = str(info.get("versionName") or "1.0").strip("'").replace("'", "")
    ver_code = str(info.get("versionCode") or "1").strip("'")

    (out / "settings.gradle").write_text(
        f"rootProject.name = \"{ns.split('.')[-1]}\"\ninclude ':app'\n", encoding="utf-8")
    (out / "build.gradle").write_text(
        "buildscript {\n"
        "    repositories { google(); mavenCentral() }\n"
        "    dependencies { classpath 'com.android.tools.build:gradle:8.5.2' }\n"
        "}\n"
        "allprojects { repositories { google(); mavenCentral() } }\n", encoding="utf-8")
    app = module
    (app / "build.gradle").write_text(
        "plugins { id 'com.android.application' }\n\n"
        f"android {{\n    namespace '{ns}'\n    compileSdk 34\n\n"
        "    defaultConfig {\n"
        f"        applicationId '{pkg}'\n"
        f"        minSdk {min_sdk if min_sdk.isdigit() else 21}\n"
        f"        targetSdk {target_sdk if target_sdk.isdigit() else 34}\n"
        f"        versionCode {ver_code if ver_code.isdigit() else 1}\n"
        f"        versionName '{ver_name}'\n    }}\n"
        "    compileOptions {\n"
        "        sourceCompatibility JavaVersion.VERSION_17\n"
        "        targetCompatibility JavaVersion.VERSION_17\n    }\n"
        "    lint { abortOnError false }\n"
        "}\n\n"
        "dependencies {\n"
        "    // 反编译代码引用的第三方库需自行补全（jadx 不会告诉你全部）\n"
        "    implementation 'androidx.appcompat:appcompat:1.7.0'\n"
        "}\n", encoding="utf-8")
    (out / "gradle.properties").write_text(
        "android.useAndroidX=true\norg.gradle.jvmargs=-Xmx2048m\n", encoding="utf-8")
    (out / "README-反编译工程.md").write_text(
        "# 反编译工程（参考骨架）\n\n"
        "由 APK2PC Studio 自动生成。\n\n"
        "## 能做什么\n"
        "- 用 Android Studio 直接打开阅读代码\n"
        "- 局部改动 + 补依赖后编译调试\n\n"
        "## 不要期待什么\n"
        "- 不能保证 `gradle assembleDebug` 一次通过：混淆、内联、资源 id、native so 依赖\n"
        "  都会让 jadx 的输出无法直编。要「改完能装回手机」，请走 smali 路线（apktool b）。\n"
        "- res/ 里 9-patch 与 value 别名可能需要手工修。\n", encoding="utf-8")
    return out


def make_launcher(apk_path: Path, info: dict, target_dir: Path) -> Path:
    """生成「双击即运行这个 APK」的独立 EXE（APK 直接内嵌在 exe 尾部）。

    冻结态：复制自身 exe 再把 APK + 配置追加到尾部，产物是单文件；
    源码态：生成 .bat 调 python app.py --launch（开发时够用）。
    """
    from . import launcher as lch

    target_dir.mkdir(parents=True, exist_ok=True)
    # 包名 / 启动 Activity 是产物能不能拉起的关键，缺了就地补（aapt2 可用时）
    if not info.get("package") or not info.get("main_activity"):
        try:
            provision.ensure_buildtools(lambda level, text: None)
        except Exception:  # noqa: BLE001
            pass
        extra = apkmod.badging_info(apk_path) or {}
        info = {**extra, **{k: v for k, v in info.items() if v}}
    name = apkmod.sanitize(info.get("label") or apk_path.stem)
    cfg = {
        "label": info.get("label", name),
        "file_name": apk_path.name,
        "package": info.get("package", ""),
        "activity": info.get("main_activity", ""),
        "avd": C.settings["avd"],
        # 默认窗口模式：-fullscreen 在部分 Windows + swiftshader 组合上会让模拟器秒退
        "fullscreen": False,
    }
    if C.is_frozen():
        import sys

        exe_path = Path(sys.executable)
        # 产物必须是「单个 exe」：拷到没装 Python 的电脑上也能双击。
        # 优先用打包时一并产出的瘦启动器模板（onefile，自带运行时、不依赖 _internal）。
        templates = [
            C.program_dir() / "launcher-base.exe",
            exe_path.parent / "launcher-base.exe",
        ]
        base = next((p for p in templates if p.is_file()), None)

        if base is None:
            # 当前进程本身就是 onefile（同目录没有 _internal）→ 自身可作模板
            if not (exe_path.parent / "_internal").is_dir():
                base = exe_path
            else:
                # onedir 兜底：整包复制，保证产物一定可用（体积略大）
                bundle = target_dir / name
                shutil.rmtree(bundle, ignore_errors=True)
                shutil.copytree(exe_path.parent, bundle)
                exe = bundle / exe_path.name
                lch.build_embedded(exe, apk_path, cfg)
                return exe

        exe = target_dir / f"{name}.exe"
        shutil.copy2(base, exe)
        lch.build_embedded(exe, apk_path, cfg)
        return exe

    bat = target_dir / f"{name}.bat"
    py = Path(sys.executable)
    main_py = C.res_root() / "app.py"
    bat.write_text(
        "@echo off\r\n"
        f'start "" "{py}" "{main_py}" --launch "{apk_path.resolve()}"\r\n',
        encoding="utf-8")
    return bat
