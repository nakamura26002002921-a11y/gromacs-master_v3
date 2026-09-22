# ============================================================
# Usage:
#   python3 createplan.py -t templates/example.json
#   python3 createplan.py -t templates/example.json -o plans/example.json
# ============================================================

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--template", required=True)
    parser.add_argument("-o", "--output")
    args = parser.parse_args()
    with open(args.template, encoding="utf-8") as f:
        text = f.read()
    params = list(dict.fromkeys(
        re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", text)
    ))

    for param in params:
        while True:
            print(f"{param}:", end="")
            value = input().strip()
            if value:
                # テンプレートはJSON文字列の中に埋め込まれるため、" や \ などを
                # エスケープしてから置換する(しないと JSON が壊れる)
                escaped = json.dumps(value, ensure_ascii=False)[1:-1]
                text = text.replace(f"{{{param}}}", escaped)
                break
            print("空です。入力してください。")

    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"plan のJSONが不正です: {e}")
    if args.output:
        output = Path(args.output)
    else:
        Path("plans").mkdir(exist_ok=True)
        output = Path("plans") / f"{datetime.now():%Y%m%d_%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Plan created: {output}")

if __name__ == "__main__":
    main()
