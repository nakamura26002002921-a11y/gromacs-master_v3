# dynamic_orchestrater.py
# ============================================================
# Usage:
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..."
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..." --assistant
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..." --assistant --server-url "https://xxxx.trycloudflare.com"
# ============================================================

import argparse
import json
import requests
import subprocess
import sys
import time
import uuid
from pathlib import Path
from groq import Groq


SYSTEM_PROMPT = """
Purpose:
A command failed. Write a command to recover from the error.

- command: The command to execute
- purpose: Why this command is necessary

All fields must strictly follow the specified JSON Schema types.
Output JSON only. Do not output any other text.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "purpose": {"type": "string"}
    },
    "required": ["command", "purpose"],
    "additionalProperties": False
}

def call_llm(history, api_key):
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(model="openai/gpt-oss-120b", 
                                              messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": json.dumps(history, ensure_ascii=False)}], 
                                              temperature=0, max_tokens=2000, 
                                              response_format={"type": "json_schema", "json_schema": {"name": "recovery_command", "strict": True, "schema": SCHEMA}})
    return json.loads(response.choices[0].message.content)


def build_assistant_prompt(history):
    commands = "\n".join(f"{{{item['実行コマンド']}}}" for item in history[:-1])
    failed = history[-1]
    return ("これまでの実行履歴:\n" f"{commands}\n" "}\n\n" "今回のコマンド:\n" f"{{{failed['実行コマンド']}}}\n\n" "エラー:\n" f"{{{failed['stderr'][:500]}}}")

def call_llm_assistant(messages, history, api_key):
    client = Groq(api_key=api_key)
    messages.append({"role": "user", "content": build_assistant_prompt(history)})
    response = client.chat.completions.create(model="openai/gpt-oss-120b", messages=messages, temperature=0, max_tokens=2000, 
                                              response_format={"type": "json_schema", "json_schema": {"name": "recovery_command", "strict": True, "schema": SCHEMA}})
    result = response.choices[0].message.content
    messages.append({"role": "assistant", "content": result})
    return json.loads(result)


def create_approval_request(server_url, request_id, command, purpose):
    response = requests.post(f"{server_url}/api/requests", json={"id": request_id, "purpose": purpose, "command": command}, timeout=10)
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
        print(f"Waiting for approval: {request_id} ({int(time.time() - started)} seconds elapsed)")
        time.sleep(interval)


def run(node, cmd, cwd):
    print(f"[{node}] Executing: {cmd['実行コマンド']}")
    result = subprocess.run(cmd["実行コマンド"], shell=True, cwd=cwd, capture_output=True, text=True)
    return {"node": node, "実行コマンド": cmd["実行コマンド"], "目的": cmd["目的"], "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}

def save_history(history, history_path, log_path):
    data = json.dumps(history, ensure_ascii=False, indent=2)
    history_path.write_text(data, encoding="utf-8")
    log_path.write_text(data, encoding="utf-8")

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
    p.add_argument("--assistant", action="store_true", help="Use assistant-format conversation history")
    a = p.parse_args()
    
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    history_path = Path(a.history) if a.history else Path("history.json")
    log_path = Path(a.logpath) if a.logpath else Path("orchestrater.log")
    history = []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    start = a.start or next(iter(plan))
    end = a.end or list(plan)[-1]
    node = start
    while True:
        result = run(node, plan[node], a.executionpath)
        history.append(result)
        save_history(history, history_path, log_path)
        if result["returncode"] == 0:
            if node == end:
                return 0
            node = plan[node]["次のノード"]
            continue
        recovered = False
        for retry in range(a.max_retries):
            if a.assistant:
                recovery_command = call_llm_assistant(messages, history, a.api_key)
            else:
                recovery_command = call_llm(history, a.api_key)
            request_id = f"{Path(a.plan).stem}_{node}_{uuid.uuid4().hex[:8]}"
            print(f"Recovery command is waiting for approval: {request_id}")
            create_approval_request(a.server_url, request_id, recovery_command["command"], recovery_command["purpose"])
            approved = wait_for_approval(a.server_url, request_id, a.approval_timeout, a.approval_interval)
            if approved is None:
                print("Approval was not received.")
                return 1
            recovery_command = {
                "実行コマンド": approved["command"],
                "目的": approved["purpose"]
            }
            print("Approval confirmed. Executing recovery command.")
            recovery_result = run(node, recovery_command, a.executionpath)
            history.append(recovery_result)
            save_history(history, history_path, log_path)
            if recovery_result["returncode"] == 0:
                recovered = True
                break
        if not recovered:
            return 1
        if node == end:
            return 0
        node = plan[node]["次のノード"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted.")
        sys.exit(130)
