"""迷你 Maven 依赖解析器（并发下载）。

为什么需要：jadx 官方 GitHub Release 在国内经常下不动，而 Maven 中央仓（repo1）速度很好。
但 Maven 上的 `jadx-cli-x.y.z-all.jar` 其实是瘦 jar（只有 cli 的 30 多个 class），
所以这里按 POM 递归解析整棵依赖树，最后用 `java -cp "lib/*" jadx.cli.JadxCLI` 启动。
"""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .runner import LogFn, download

REPO = "https://repo1.maven.org/maven2"
NS = "{http://maven.apache.org/POM/4.0.0}"

# 开发期/测试期依赖，永远不需要
SKIP_ARTIFACTS = {
    "junit", "junit-jupiter", "mockito-core", "hamcrest", "hamcrest-core",
    "slf4j-simple", "maven-resolver-test-util", "kotlin-test", "kotlin-test-junit",
    "powermock", "truth", "assertj-core",
}
SKIP_SCOPES = {"test", "provided", "system", "import"}


def _url(g: str, a: str, v: str, ext: str) -> str:
    return f"{REPO}/{g.replace('.', '/')}/{a}/{v}/{a}-{v}.{ext}"


def _text(node, tag: str) -> str:
    if node is None:
        return ""
    el = node.find(NS + tag)
    return (el.text or "").strip() if el is not None else ""


def _subst(value: str, props: dict, depth: int = 0) -> str:
    if not value or "${" not in value or depth > 6:
        return value
    out = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
    return _subst(out, props, depth + 1)


class Resolver:
    def __init__(self, cache_dir: Path, log: LogFn) -> None:
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log
        self._poms: dict[tuple, ET.Element | None] = {}
        self._props: dict[tuple, dict] = {}

    # ---- POM
    def pom(self, g: str, a: str, v: str) -> ET.Element | None:
        key = (g, a, v)
        if key in self._poms:
            return self._poms[key]
        f = self.cache / f"{g}-{a}-{v}.pom"
        if not f.exists():
            try:
                req = urllib.request.Request(_url(g, a, v, "pom"),
                                             headers={"User-Agent": "APK2PC/1.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    f.write_bytes(r.read())
            except Exception:
                f.write_bytes(b"")
        root = None
        try:
            data = f.read_bytes()
            if data.strip():
                root = ET.fromstring(data)
        except Exception:
            root = None
        self._poms[key] = root
        return root

    def props(self, g: str, a: str, v: str, depth: int = 0) -> dict:
        key = (g, a, v)
        if key in self._props:
            return self._props[key]
        pom = self.pom(g, a, v)
        p = {"project.version": v, "project.groupId": g, "project.artifactId": a,
             "version": v, "pom.version": v, "pom.groupId": g}
        if pom is not None:
            parent = pom.find(NS + "parent")
            if parent is not None and depth < 4:
                pg = _text(parent, "groupId") or p.get("project.groupId", "")
                pa, pv = _text(parent, "artifactId"), _text(parent, "version")
                if pg and pa and pv:
                    p.update({k: val for k, val in self.props(pg, pa, pv, depth + 1).items()
                              if k not in p})
            pel = pom.find(NS + "properties")
            if pel is not None:
                for child in list(pel):
                    p[child.tag.replace(NS, "")] = (child.text or "").strip()
        for _ in range(4):
            changed = False
            for k, val in list(p.items()):
                nv = _subst(val, p)
                if nv != val:
                    p[k] = nv
                    changed = True
            if not changed:
                break
        self._props[key] = p
        return p

    # ---- 依赖树（先只走 POM，很快）
    def tree(self, root: tuple[str, str, str]) -> list[tuple[str, str, str]]:
        queue: list[tuple] = [(*root, "compile", 0)]
        seen: set[tuple] = set()
        out: list[tuple] = []
        while queue:
            g, a, v, scope, depth = queue.pop(0)
            if depth > 8 or not (g and a and v):
                continue
            key = (g, a, v)
            if key in seen or scope in SKIP_SCOPES or a in SKIP_ARTIFACTS:
                continue
            seen.add(key)
            pom = self.pom(g, a, v)
            if pom is None:
                continue
            out.append(key)
            p = self.props(g, a, v)

            managed: dict[tuple, str] = {}
            dm = pom.find(NS + "dependencyManagement")
            if dm is not None:
                for dep in dm.iter(NS + "dependency"):
                    dg = _subst(_text(dep, "groupId"), p)
                    da = _subst(_text(dep, "artifactId"), p)
                    dv = _subst(_text(dep, "version"), p)
                    if dg and da:
                        managed[(dg, da)] = dv

            deps = pom.find(NS + "dependencies")
            if deps is None:
                continue
            for dep in deps.findall(NS + "dependency"):
                dg = _subst(_text(dep, "groupId"), p)
                da = _subst(_text(dep, "artifactId"), p)
                dv = (_subst(_text(dep, "version"), p) or managed.get((dg, da), ""))
                ds = _text(dep, "scope") or "compile"
                if (_text(dep, "optional") or "false").lower() == "true":
                    continue
                if not dv:
                    continue        # 版本由上层管理，拿不到就跳过（多为测试依赖）
                queue.append((dg, da, dv, ds, depth + 1))
        return out


def _fetch_one(g: str, a: str, v: str, dest: Path, log: LogFn) -> Path | None:
    jar = dest / f"{a}-{v}.jar"
    if jar.exists() and jar.stat().st_size > 1024:
        return jar
    try:
        download(_url(g, a, v, "jar"), jar, log, retries=1, progress=False)
        return jar if jar.exists() and jar.stat().st_size > 1024 else None
    except Exception as e:  # noqa: BLE001
        log("warn", f"  跳过 {a}:{v} ({e})")
        return None


def fetch_tree(root: tuple[str, str, str], dest: Path, log: LogFn, workers: int = 8) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    log("info", "解析 Maven 依赖树（repo1.maven.org）…")
    r = Resolver(dest.parent / "_pomcache", log)
    items = r.tree(root)
    todo = [it for it in items if not (dest / f"{it[1]}-{it[2]}.jar").exists()]
    log("info", f"依赖 {len(items)} 个，需下载 {len(todo)} 个（{workers} 线程并发）")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_one, g, a, v, dest, log): (g, a, v) for g, a, v in todo}
        for fu in as_completed(futs):
            done += 1
            if done % 5 == 0 or done == len(todo):
                log("progress", f"  依赖下载 {done}/{len(todo)}")

    # 并发下载偶发失败时补一次（串行重试），否则会出现「少一个 jar → 静默失败」
    def _ok(it) -> bool:
        p = dest / f"{it[1]}-{it[2]}.jar"
        return p.exists() and p.stat().st_size > 1024

    missing = [it for it in items if not _ok(it)]
    for g, a, v in missing:
        log("warn", f"重试 {a}:{v}")
        _fetch_one(g, a, v, dest, log)
    still = [it for it in items if not _ok(it)]
    if still:
        log("warn", "以下 jar 仍缺失（可能影响部分功能）: "
                    + ", ".join(f"{a}:{v}" for _, a, v in still))

    jars = sorted(dest.glob("*.jar"))
    if not jars:
        raise RuntimeError("依赖解析后没有任何 jar")
    log("ok", f"依赖就绪 {len(jars)} 个 jar")
    return jars
