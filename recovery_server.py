# recovery_server.py
# ============================================================
# Usage:
#   1. 復旧APIで使用するAPIキーを環境変数に設定
#      export RECOVERY_API_KEY="$(openssl rand -hex 32)"
#
#   2. 承認ページのURLを設定
#      export APPROVAL_URL="https://YOUR-USER.github.io/recovery_approval-v1/"
#
#   3. LLMで復旧コマンドを自動生成するためのGroq APIキーを設定
#      export GROQ_API_KEY="gsk_..."
#
#   4. (任意) 承認の有効期限を秒で設定 (デフォルト 3600)
#      export APPROVAL_TTL=3600
#
#   5. (任意) 承認ページからのアクセスを許可するオリジンをカンマ区切りで設定
#      未設定の場合は APPROVAL_URL のオリジン(scheme://host)のみ許可する。
#      export CORS_ORIGINS="https://YOUR-USER.github.io"
#
#   6. (任意) 復旧サーバーの公開URLを設定
#      未設定の場合は、オーケストレーターがアクセスしてきたURL(Host / X-Forwarded-*)から
#      自動で決定し、承認URLの api= に付与する。
#      export PUBLIC_URL="https://xxxx.trycloudflare.com"
#
#   7. 復旧サーバーを起動
#      python3 recovery_server.py
#
#   8. Cloudflare Tunnel等でHTTPS公開
#      cloudflared tunnel --url http://127.0.0.1:5000
# ============================================================

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from flask import Flask, request, jsonify
from flask_cors import CORS
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


def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"環境変数 {name} が設定されていません。ファイル冒頭の Usage を参照してください。")
    return value


API_KEY = require_env("RECOVERY_API_KEY")
APPROVAL_URL = require_env("APPROVAL_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
try:
    APPROVAL_TTL = int(os.environ.get("APPROVAL_TTL", "3600"))
except ValueError:
    sys.exit("環境変数 APPROVAL_TTL は整数(秒)で指定してください。")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")


def get_cors_origins():
    configured = [o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    if configured:
        return configured
    parts = urlsplit(APPROVAL_URL)
    return [f"{parts.scheme}://{parts.netloc}"]


app = Flask(__name__)
CORS(app, origins=get_cors_origins())

pending_requests = {}


def safe_equal(a, b):
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def check_api_key():
    return safe_equal(request.headers.get("X-API-Key", ""), API_KEY)


def command_hash(command):
    canonical = json.dumps({"実行コマンド": command["実行コマンド"], "目的": command["目的"]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def call_vocab(history, api_key):
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"エラー:\n{history}"}
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


# ---- 承認トークンの総当たり対策: request_id ごとの失敗回数を制限する ----
MAX_FAILED_TOKENS = 5


def is_expired(item):
    return time.time() - item["created_at"] > APPROVAL_TTL


def get_public_url():
    if PUBLIC_URL:
        return PUBLIC_URL
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip()
    host = request.headers.get("X-Forwarded-Host", request.host).split(",")[0].strip()
    return f"{scheme}://{host}"


def build_approval_url(request_id, approval_token):
    parts = urlsplit(APPROVAL_URL)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("request_id", "token", "api")]
    query += [("request_id", request_id), ("api", get_public_url())]
    fragment = urlencode({"token": approval_token})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), fragment))


def validate_history(history):
    if not isinstance(history, list) or not history:
        return "history は空でない配列で送ってください"
    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            return f"history[{i}] はオブジェクトである必要があります"
    if not isinstance(history[-1].get("ノード"), str):
        return "history の最後の要素に文字列の「ノード」が必要です"
    return None


def get_cmd(history):
    last = history[-1]
    command = {"実行コマンド": "echo recovery", "目的": f"{last['ノード']}の復旧処理"}
    if GROQ_API_KEY:
        try:
            command = call_vocab(history, GROQ_API_KEY)
        except Exception as e:
            print(f"LLMによる復旧コマンド生成に失敗したため固定コマンドを使います: {e}")
    request_id = secrets.token_urlsafe(32)
    approval_token = secrets.token_urlsafe(32)
    pending_requests[request_id] = {
        "history": history,
        "command": command,
        "command_hash": command_hash(command),
        "approval_token": approval_token,
        "status": "pending",
        "created_at": time.time(),
        "failed_tokens": 0,
        "delivered": False,
    }
    return {"status": "pending", "request_id": request_id, "approval_url": build_approval_url(request_id, approval_token)}


def get_valid_item(request_id, token):
    item = pending_requests.get(request_id)
    if item is None:
        return None, (jsonify({"status": "not_found"}), 404)
    if item["failed_tokens"] >= MAX_FAILED_TOKENS:
        return None, (jsonify({"status": "locked"}), 429)
    if not safe_equal(token, item["approval_token"]):
        item["failed_tokens"] += 1
        return None, (jsonify({"status": "unauthorized"}), 401)
    if item["status"] == "pending" and is_expired(item):
        item["status"] = "expired"
    return item, None


def decide(request_id, new_status):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    item, error = get_valid_item(request_id, body.get("token"))
    if error:
        return error
    if item["status"] != "pending":
        return jsonify({"status": item["status"]}), 409
    if new_status == "approved" and not safe_equal(body.get("command_hash"), item["command_hash"]):
        return jsonify({"status": "hash_mismatch"}), 400
    item["status"] = new_status
    return jsonify({"status": new_status})


@app.route("/", methods=["POST"])
def recovery():
    if not check_api_key():
        return jsonify({"エラー": "Unauthorized"}), 401
    history = request.get_json(silent=True)
    error = validate_history(history)
    if error:
        return jsonify({"エラー": error}), 400
    return jsonify(get_cmd(history))


@app.route("/result/<request_id>", methods=["GET"])
def result(request_id):
    if not check_api_key():
        return jsonify({"エラー": "Unauthorized"}), 401
    item = pending_requests.get(request_id)
    if item is None:
        return jsonify({"status": "not_found"}), 404
    if item["status"] == "pending" and is_expired(item):
        item["status"] = "expired"
    if item["status"] == "approved":
        if item["delivered"]:
            return jsonify({"status": "consumed"}), 410
        item["delivered"] = True
        return jsonify({"status": "approved", "command": item["command"], "command_hash": item["command_hash"]})
    return jsonify({"status": item["status"]})


@app.route("/api/request/<request_id>", methods=["GET"])
def get_request(request_id):
    item, error = get_valid_item(request_id, request.args.get("token"))
    if error:
        return error
    return jsonify({"status": item["status"], "history": item["history"], "command": item["command"], "command_hash": item["command_hash"]})


@app.route("/api/request/<request_id>/approve", methods=["POST"])
def approve(request_id):
    return decide(request_id, "approved")


@app.route("/api/request/<request_id>/reject", methods=["POST"])
def reject(request_id):
    return decide(request_id, "rejected")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
