# dynamic_orchestrater.py
# ============================================================
# Usage:
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..."
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..." --server-url "https://xxxx.trycloudflare.com"
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..." -s nvt -e npt_pr -ep /path/to/workdir --server-url "https://xxxx.trycloudflare.com"
# ============================================================

import argparse
import json
import requests
import subprocess
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime
from groq import Groq


SYSTEM_PROMPT = """
目的：
エラーが起きたから、復元のコマンドを書いて

- 実行コマンド: 入力するコマンド
- 目的: なぜそのコマンドなのか?

すべてのフィールドは指定されたJSON Schemaの型を厳密に守る。
JSON以外の文章を出力しない。
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "実行コマンド": {"type": "string"},
        "目的": {"type": "string"}
    },
    "required": ["実行コマンド", "目的"],
    "additionalProperties": False
}


def call_vocab(history, api_key):
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(history, ensure_ascii=False)}
        ],
        temperature=0,
        max_tokens=2000,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "recovery_command",
                "strict": True,
                "schema": SCHEMA
            }
        }
    )
    return json.loads(response.choices[0].message.content)


def create_approval_request(server_url, request_id, command, purpose):
    response = requests.post(f"{server_url}/api/requests", json={"id": request_id, "command": command, "purpose": purpose}, timeout=10)
    return response.json()


def get_approval_request(server_url, request_id):
    response = requests.get(f"{server_url}/api/requests/{request_id}", timeout=10)
    return response.json()


def wait_for_approval(server_url, request_id, timeout, interval):
    started = time.time()
    while True:
        data = get_approval_request(server_url, request_id)
        if data["status"] == "承認済み":
            return data
        if time.time() - started >= timeout:
            return None
        print(f"承認待ち: {request_id} ({int(time.time() - started)}秒経過)")
        time.sleep(interval)


def run(node, cmd, cwd):
    print(f"[{node}] 実行: {cmd['実行コマンド']}")
    result = subprocess.run(cmd["実行コマンド"], shell=True, cwd=cwd, capture_output=True, text=True)
    return {
        "node": node,
        "実行コマンド": cmd["実行コマンド"],
        "目的": cmd["目的"],
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-p", "--plan", required=True)
    p.add_argument("--api-key", required=True)
    p.add_argument("--history")
    p.add_argument("-l", "--logpath")
    p.add_argument("-s", "--start")
    p.add_argument("-e", "--end")
    p.add_argument("-ep", "--executionpath", default=".")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--server-url", required=True)
    p.add_argument("--approval-timeout", type=int, default=3600)
    p.add_argument("--approval-interval", type=int, default=5)
    a = p.parse_args()

    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    history_path = Path(a.history) if a.history else Path("history.json")
    log_path = Path(a.logpath) if a.logpath else Path("orchestrater.log")
    history = []
    start = a.start or next(iter(plan))
    end = a.end or list(plan)[-1]
    node = start

    while True:
        result = run(node, plan[node], a.executionpath)
        history.append(result)
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        log_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

        if result["returncode"] == 0:
            if node == end:
                return 0
            node = plan[node]["next"]
            continue

        recovery_command = None

        for retry in range(a.max_retries):
            recovery_command = call_vocab(history, a.api_key)
            request_id = f"{Path(a.plan).stem}_{node}_{uuid.uuid4().hex[:8]}"

            print(f"復旧コマンドを承認待ちにします: {request_id}")
            create_approval_request(a.server_url, request_id, recovery_command["実行コマンド"], recovery_command["目的"])

            approved = wait_for_approval(a.server_url, request_id, a.approval_timeout, a.approval_interval)

            if approved is None:
                print("承認されませんでした。")
                return 1

            if approved["status"] != "承認済み":
                print("承認状態を確認できませんでした。")
                return 1

            recovery_command = {
                "実行コマンド": approved["command"],
                "目的": approved["purpose"]
            }

            print("承認済みを確認しました。復旧コマンドを実行します。")
            recovery_result = run(node, recovery_command, a.executionpath)
            history.append(recovery_result)
            history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            log_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

            if recovery_result["returncode"] == 0:
                break

        else:
            return 1

        if history[-1]["returncode"] != 0:
            return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("中断されました。")
        sys.exit(130)
