# dynamic_orchestrater.py
#===========================================================
# Usage:
#   export RECOVERY_API_KEY="..."
#   python3 dynamic_orchestrater.py -p plan.json --recovery-url https://example.com
#   python3 dynamic_orchestrater.py -p plan.json -s nvt -e npt_pr -ep /path/to/workdir --recovery-url https://example.com
#===========================================================

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from datetime import datetime

POLL = 5


def command_hash(cmd):
    canonical = json.dumps({"実行コマンド": cmd["実行コマンド"], "目的": cmd["目的"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def api(method, url, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"X-API-Key": os.environ["RECOVERY_API_KEY"], "User-Agent": "gromacs-orchestrator/2.0", "Content-Type": "application/json"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception as e:
        print(f"復旧サーバーに接続できません: {e}")
        return {}


def wait_for_approval(history, url, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = api("POST", url + "/", history)
        if res.get("request_id"):
            print("承認ページ: " + res["approval_url"])
            rid = res["request_id"]
            break
        time.sleep(POLL)
    else:
        return {"status": "timeout"}
    while time.time() < deadline:
        res = api("GET", url + "/result/" + rid)
        if res.get("status") not in (None, "pending"):
            return res
        time.sleep(POLL)
    return {"status": "timeout"}


def run(node, cmd, cwd):
    r = subprocess.run(cmd["実行コマンド"], shell=True, cwd=cwd, capture_output=True, text=True)
    return {"ノード": node, "実行コマンド": cmd["実行コマンド"], "目的": cmd["目的"], "出力": r.stdout, "エラー": r.stderr, "終了コード": r.returncode}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-p", "--plan", required=True)
    p.add_argument("--history")
    p.add_argument("-l", "--logpath")
    p.add_argument("-s", "--start")
    p.add_argument("-e", "--end")
    p.add_argument("-ep", "--executionpath", default=".")
    p.add_argument("--recovery-url", default="")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--timeout", type=int, default=3600)
    a = p.parse_args()
    plan = json.load(open(a.plan, encoding="utf-8"))
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = [Path(a.history or f"histories/{Path(a.plan).stem}_{now}.json"), Path(a.logpath or f"logs/{Path(a.plan).stem}_{now}.json")]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
    node = a.start or next(iter(plan))
    end = a.end or list(plan)[-1]
    history = []
    retries = 0
    try:
        while True:
            history.append(run(node, plan[node], a.executionpath))
            if history[-1]["終了コード"] == 0:
                if node == end:
                    return True
                retries = 0
                node = plan[node]["次のノード"]
                continue
            if not a.recovery_url or retries >= a.max_retries:
                return False
            retries += 1
            res = wait_for_approval(history, a.recovery_url.rstrip("/"), a.timeout)
            cmd = res.get("command")
            if res["status"] != "approved" or not hmac.compare_digest(res.get("command_hash", ""), command_hash(cmd)):
                print("復旧コマンドが承認されなかった、または内容が一致しないため停止します: " + res["status"])
                return False
            history.append(run(node, cmd, a.executionpath))
            if history[-1]["終了コード"] != 0:
                return False
    finally:
        for path in paths:
            json.dump(history, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    try:
        sys.exit(0 if main() else 1)
    except KeyboardInterrupt:
        print("中断されました。")
        sys.exit(130)
