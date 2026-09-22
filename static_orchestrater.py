# ============================================================
# Usage:
#   python3 static_orchestrater.py -p plan.json
#   python3 static_orchestrater.py -p plan.json -s nvt -e npt_pr
#   python3 static_orchestrater.py -p plan.json -ep /path/to/workdir
#   python3 static_orchestrater.py -p plan.json --history histories/test.json -l logs/test.json
# ============================================================

import argparse
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime


def validate_plan(plan, start, end):
    if not isinstance(plan, dict) or not plan:
        raise SystemExit("plan が空、またはオブジェクトではありません")
    for name, n in plan.items():
        if not isinstance(n, dict):
            raise SystemExit(f"ノード '{name}' がオブジェクトではありません")
        for key in ("実行コマンド", "目的"):
            if not isinstance(n.get(key), str):
                raise SystemExit(f"ノード '{name}' に文字列の「{key}」がありません")
        nxt = n.get("次のノード")
        if nxt is not None and nxt not in plan:
            raise SystemExit(f"ノード '{name}' の「次のノード」'{nxt}' は plan に存在しません")
    for label, value in (("--start", start), ("--end", end)):
        if value is not None and value not in plan:
            raise SystemExit(f"{label} のノード '{value}' は plan に存在しません。使えるノード: {', '.join(plan)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-p", "--plan", required=True)
    p.add_argument("--history")
    p.add_argument("-l", "--logpath")
    p.add_argument("-s", "--start")
    p.add_argument("-e", "--end")
    p.add_argument("-ep", "--executionpath")
    a = p.parse_args()
    plan_path = Path(a.plan)
    plan = json.load(open(plan_path, encoding="utf-8"))
    name = plan_path.stem
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    history_path = Path(a.history or f"histories/{name}_{now}.json")
    log_path = Path(a.logpath or f"logs/{name}_{now}.json")
    execution_path = Path(a.executionpath or ".")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    validate_plan(plan, a.start, a.end)
    nodes = list(plan.keys())
    node = a.start or nodes[0]
    end = a.end or nodes[-1]
    history = []
    succeeded = False
    try:
        while True:
            n = plan[node]
            try:
                r = subprocess.run(n["実行コマンド"], shell=True, cwd=execution_path, capture_output=True, text=True)
                result = {"ノード": node, "実行コマンド": n["実行コマンド"], "目的": n["目的"], "出力": r.stdout, "エラー": r.stderr, "終了コード": r.returncode}
            except KeyboardInterrupt:
                result = {"ノード": node, "実行コマンド": n["実行コマンド"], "目的": n["目的"], "出力": "", "エラー": "KeyboardInterrupt (^C)", "終了コード": -2}
                history.append(result)
                raise
            history.append(result)
            if r.returncode != 0:
                break
            if node == end:
                succeeded = True
                break
            node = n["次のノード"]
    finally:
        json.dump(history, open(history_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        json.dump(history, open(log_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return succeeded

if __name__ == "__main__":
    # 最後まで(または -e のノードまで)成功したら 0、失敗して止まったら 1 を返す。^C は 130
    try:
        sys.exit(0 if main() else 1)
    except KeyboardInterrupt:
        print("中断されました。history は保存済みです。")
        sys.exit(130)
