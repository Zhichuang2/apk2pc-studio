"""端到端验证：解包 → Java → 重建 → 签名。用法: python tests/e2e.py <apk> [action]"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import pipeline  # noqa: E402

apk = sys.argv[1] if len(sys.argv) > 1 else str(ROOT.parent / "_test" / "test.apk")
action = sys.argv[2] if len(sys.argv) > 2 else "decode"

params = {"apk": apk}
if action == "build":
    decoded = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if not decoded or not decoded.exists():
        raise SystemExit("build 需要传入已解包目录: e2e.py <apk> build <src-dir>")
    params["decoded"] = str(decoded)
    params["workspace"] = str(decoded.parent)

jm = pipeline.JobManager()
job = jm.start(action, params)
cursor = 0
while True:
    snap = job.to_dict(cursor)
    for line in snap["logs"]:
        print(f"[{line['level']:8}] {line['text']}", flush=True)
    cursor = snap["cursor"]
    if snap["status"] != "running":
        print("\n=== STATUS:", snap["status"], "| elapsed", snap["elapsed"], "s")
        for s in snap["steps"]:
            print(f"  {s['status']:8} {s['name']}  {s['detail']}")
        print("RESULT:", {k: v for k, v in snap["result"].items() if k != "peek"})
        if snap["error"]:
            print("ERROR:", snap["error"])
        break
    time.sleep(0.7)
