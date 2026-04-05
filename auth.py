"""
Tesla OAuth 2.0 認証モジュール
PKCE対応のOAuth認証フローを管理し、トークンをローカルファイルに保存する
"""
import json
import os
import secrets
import hashlib
import base64
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

TESLA_CLIENT_ID = os.getenv("TESLA_CLIENT_ID")
TESLA_CLIENT_SECRET = os.getenv("TESLA_CLIENT_SECRET")
TESLA_REDIRECT_URI = os.getenv("TESLA_REDIRECT_URI")
TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TESLA_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"

TOKENS_FILE = Path("tokens.json")

# Tesla API に必要なスコープ
SCOPES = "openid offline_access vehicle_device_data vehicle_charging_cmds"


def generate_pkce_pair() -> tuple[str, str]:
    """PKCE用のcode_verifierとcode_challengeを生成"""
    code_verifier = secrets.token_urlsafe(96)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return code_verifier, code_challenge


def get_authorization_url(state: str, code_challenge: str) -> str:
    """Tesla認証ページのURLを生成"""
    params = {
        "client_id": TESLA_CLIENT_ID,
        "redirect_uri": TESLA_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{TESLA_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str, code_verifier: str) -> dict:
    """認証コードをアクセストークンに交換"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            TESLA_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": TESLA_CLIENT_ID,
                "client_secret": TESLA_CLIENT_SECRET,
                "code": code,
                "redirect_uri": TESLA_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
        )
        response.raise_for_status()
        tokens = response.json()
        # 有効期限を絶対時刻で保存
        tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        save_tokens(tokens)
        return tokens


async def refresh_access_token(refresh_token: str) -> dict:
    """リフレッシュトークンでアクセストークンを更新"""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            TESLA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": TESLA_CLIENT_ID,
                "client_secret": TESLA_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        tokens = response.json()
        tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        save_tokens(tokens)
        return tokens


def save_tokens(tokens: dict) -> None:
    """トークンをファイルに保存（.gitignore対象）"""
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def load_tokens() -> dict | None:
    """保存済みトークンを読み込む"""
    if not TOKENS_FILE.exists():
        return None
    return json.loads(TOKENS_FILE.read_text())


async def get_valid_access_token() -> str | None:
    """有効なアクセストークンを返す（必要に応じてリフレッシュ）"""
    tokens = load_tokens()
    if not tokens:
        return None

    # 有効期限の60秒前にリフレッシュ
    if time.time() >= tokens.get("expires_at", 0) - 60:
        try:
            tokens = await refresh_access_token(tokens["refresh_token"])
        except Exception:
            return None

    return tokens.get("access_token")


def is_authenticated() -> bool:
    """認証済みかどうかを確認"""
    tokens = load_tokens()
    return tokens is not None and "refresh_token" in tokens
