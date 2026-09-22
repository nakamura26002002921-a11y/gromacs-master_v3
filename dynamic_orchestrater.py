# dynamic_orchestrater.py
# ============================================================
# Usage:
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..."
#   python3 dynamic_orchestrater.py -p plan.json --api-key "gsk_..." -s nvt -e npt_pr -ep /path/to/workdir
# ============================================================

import argparse
import json
import os
import subprocess
import sys
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
        "目的": {"type": "string"},
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
            {"role": "user", "content": f"エラー:\n{json.dumps(history, ensure_ascii=False, indent=2)}"}
        ],
        temperature=0,
        max_tokens=2000,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "recovery",
                "strict": True,
                "schema": SCHEMA
            }
        }
    )
    usage = response.usage
    print(f"  tokens: prompt={usage.prompt_tokens}, completion={usage.completion_tokens}, total={usage.total_tokens}")
    return json.loads(response.choices[0].message.content)


def run(node, cmd, cwd):
    r = subprocess.run(cmd["実行コマンド"], shell=True, cwd=cwd, capture_output=True, text=True)
    return {"ノード": node, "実行コマンド": cmd["実行コマンド"], "目的": cmd["目的"], "出力": r.stdout, "エラー": r.stderr, "終了コード": r.returncode}


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
    while True:
        history.append(run(node, plan[node], a.executionpath))
        if history[-1]["終了コード"] == 0:
            if node == end:
                break
            retries = 0
            node = plan[node]["次のノード"]
            continue
        if retries >= a.max_retries:
            break
        retries += 1
        recovery_command = call_vocab(history, a.api_key)
        print("生成された復旧コマンド: " + recovery_command["実行コマンド"])
        print("復旧コマンドの目的: " + recovery_command["目的"])
        history.append(run(node, recovery_command, a.executionpath))
        if history[-1]["終了コード"] == 0:
            retries = 0
    for path in paths:
        json.dump(history, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return history[-1]["終了コード"] == 0 and node == end


if __name__ == "__main__":
    try:
        sys.exit(0 if main() else 1)
    except KeyboardInterrupt:
        print("中断されました。")
        sys.exit(130)
