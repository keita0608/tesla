"""
Tesla OAuth 2.0 認証モジュール
トークンをSecret Managerに永続化（Cloud Run再起動後も維持）
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
GCP_PROJECT = os.getenv("GCP_PROJECT", "apt-hold-492414-r6")
SECRET_NAME = f"projects/{GCP_PROJECT}/secrets/tesla-tokens/versions/latest"

SCOPES = "openid offline_access vehicle_device_data vehicle_charging_cmds"


def generate_pkce_pair() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(96)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return code_verifier, code_challenge


def get_authorization_url(state: str, code_challenge: str) -> str:
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
        tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        save_tokens(tokens)
        return tokens


async def refresh_access_token(refresh_token: str) -> dict:
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


def _load_from_secret_manager() -> dict | None:
    """Secret Manager からトークンを読み込む"""
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=SECRET_NAME)
        return json.loads(response.payload.data.decode("utf-8"))
    except Exception:
        return None


def _save_to_secret_manager(tokens: dict) -> None:
    """Secret Manager にトークンを保存（新バージョンを追加）"""
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        secret_path = f"projects/{GCP_PROJECT}/secrets/tesla-tokens"
        client.add_secret_version(
            parent=secret_path,
            payload={"data": json.dumps(tokens).encode("utf-8")},
        )
    except Exception as e:
        print(f"[Auth] Secret Manager保存エラー: {e}")


def save_tokens(tokens: dict) -> None:
    """トークンをローカルファイルとSecret Managerに保存"""
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    _save_to_secret_manager(tokens)


def load_tokens() -> dict | None:
    """トークンを読み込む（ローカル→Secret Managerの順）"""
    if TOKENS_FILE.exists():
        try:
            return json.loads(TOKENS_FILE.read_text())
        except Exception:
            pass
    # ローカルになければSecret Managerから取得
    tokens = _load_from_secret_manager()
    if tokens:
        TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    return tokens


async def get_valid_access_token() -> str | None:
    tokens = load_tokens()
    if not tokens:
        return None
    if time.time() >= tokens.get("expires_at", 0) - 60:
        try:
            tokens = await refresh_access_token(tokens["refresh_token"])
        except Exception:
            return None
    return tokens.get("access_token")


def is_authenticated() -> bool:
    tokens = load_tokens()
    return tokens is not None and "refresh_token" in tokens
