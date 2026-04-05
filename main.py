"""
Tesla データ収集 Web アプリ
FastAPI + Tesla Fleet API + Google Sheets
Cloud Run / Cloud Scheduler 対応
"""
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from dotenv import load_dotenv

from auth import (
    generate_pkce_pair,
    get_authorization_url,
    exchange_code_for_tokens,
    is_authenticated,
    load_tokens,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Cloud Scheduler からのリクエストを認証するシークレット
SCHEDULER_SECRET = os.getenv("SCHEDULER_SECRET", "")

# PKCE用の一時データ（メモリ内・シングルプロセス前提）
_pkce_store: dict[str, str] = {}  # state -> code_verifier


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Tesla Data Collector 起動")
    yield
    logger.info("Tesla Data Collector 停止")


app = FastAPI(
    title="Tesla Data Collector",
    description="Tesla車両データをGoogle Sheetsに自動記録するアプリ",
    lifespan=lifespan,
)


# ─── ステータスページ ────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    """トップページ：認証状態と操作メニューを表示"""
    from sheets import check_connection
    authenticated = is_authenticated()
    sheets_ok = check_connection()

    auth_status = "✅ 認証済み" if authenticated else "❌ 未認証"
    sheets_status = "✅ 接続OK" if sheets_ok else "❌ 接続エラー"

    auth_action = (
        '<p>✅ Teslaと連携済みです</p>'
        if authenticated
        else '<p><a href="/auth/login" style="background:#e82127;color:white;padding:10px 20px;border-radius:5px;text-decoration:none;">🔑 Tesla でログイン</a></p>'
    )

    return f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Tesla Data Collector</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; background: #1a1a1a; color: #fff; }}
            h1 {{ color: #e82127; }}
            .card {{ background: #2a2a2a; border-radius: 10px; padding: 20px; margin: 15px 0; }}
            .status {{ display: flex; gap: 20px; }}
            .btn {{ display: inline-block; background: #444; color: white; padding: 10px 20px; border-radius: 5px; text-decoration: none; margin: 5px; cursor: pointer; }}
            .btn:hover {{ background: #555; }}
            .btn-primary {{ background: #e82127; }}
            .btn-primary:hover {{ background: #c41a20; }}
        </style>
    </head>
    <body>
        <h1>⚡ Tesla Data Collector</h1>

        <div class="card">
            <h2>接続状態</h2>
            <div class="status">
                <div>Tesla API: {auth_status}</div>
                <div>Google Sheets: {sheets_status}</div>
            </div>
            {auth_action}
        </div>

        <div class="card">
            <h2>充電履歴</h2>
            <p>Tesla APIから充電履歴を取得してGoogle Sheetsに保存します。</p>
            <a href="/charging/sync" class="btn btn-primary">🔋 充電履歴を同期</a>
            <a href="/charging/history" class="btn">📋 充電履歴を表示（JSON）</a>
        </div>

        <div class="card">
            <h2>オドメータ</h2>
            <p>現在のオドメータを取得してGoogle Sheetsに保存します。<br>
            ※ 毎日 23:59 JST に Cloud Scheduler が自動記録します。</p>
            <a href="/odometer/now" class="btn btn-primary">🚗 今すぐオドメータを記録</a>
            <a href="/odometer/current" class="btn">📊 現在値を表示（JSON）</a>
        </div>
    </body>
    </html>
    """


# ─── 認証 ──────────────────────────────────────────────────────


@app.get("/auth/login")
async def auth_login():
    """Tesla OAuth認証を開始"""
    state = secrets.token_urlsafe(16)
    code_verifier, code_challenge = generate_pkce_pair()
    _pkce_store[state] = code_verifier

    url = get_authorization_url(state, code_challenge)
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(
    code: str = Query(...),
    state: str = Query(...),
    error: str = Query(None),
):
    """Tesla OAuthコールバック処理"""
    if error:
        raise HTTPException(status_code=400, detail=f"認証エラー: {error}")

    code_verifier = _pkce_store.pop(state, None)
    if not code_verifier:
        raise HTTPException(status_code=400, detail="不正なstateパラメータです。再度ログインしてください。")

    try:
        await exchange_code_for_tokens(code, code_verifier)
        return RedirectResponse("/?auth=success")
    except Exception as e:
        logger.error(f"トークン取得エラー: {e}")
        raise HTTPException(status_code=500, detail=f"トークン取得に失敗しました: {e}")


@app.get("/auth/status")
async def auth_status():
    """認証状態を確認"""
    tokens = load_tokens()
    if not tokens:
        return {"authenticated": False}
    return {"authenticated": True, "has_refresh_token": "refresh_token" in tokens}


# ─── 充電履歴 ──────────────────────────────────────────────────


@app.get("/charging/history")
async def charging_history():
    """充電履歴をJSON形式で返す"""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="Tesla未認証。/auth/login からログインしてください。")
    from tesla_api import get_charging_history
    sessions = await get_charging_history()
    return {"count": len(sessions), "sessions": sessions}


@app.get("/charging/sync", response_class=HTMLResponse)
async def charging_sync():
    """充電履歴をGoogle Sheetsに同期"""
    if not is_authenticated():
        return RedirectResponse("/auth/login")
    from tesla_api import get_charging_history
    from sheets import save_charging_history

    try:
        sessions = await get_charging_history()
        added = save_charging_history(sessions)
        return f"""
        <html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ 充電履歴同期完了</h2>
        <p>取得件数: {len(sessions)}件</p>
        <p>新規追加: {added}件</p>
        <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
        </body></html>
        """
    except Exception as e:
        logger.error(f"充電履歴同期エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── オドメータ ────────────────────────────────────────────────


@app.get("/odometer/current")
async def odometer_current():
    """現在のオドメータをJSON形式で返す"""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="Tesla未認証。/auth/login からログインしてください。")
    from tesla_api import get_odometer
    return await get_odometer()


@app.get("/odometer/now", response_class=HTMLResponse)
async def odometer_now():
    """オドメータを今すぐ取得してGoogle Sheetsに保存"""
    if not is_authenticated():
        return RedirectResponse("/auth/login")
    from tesla_api import get_odometer
    from sheets import save_odometer

    try:
        data = await get_odometer()
        save_odometer(data["odometer_km"], data["vehicle_name"])
        return f"""
        <html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ オドメータ記録完了</h2>
        <p>車両: {data['vehicle_name']}</p>
        <p>オドメータ: {data['odometer_km']:,} km</p>
        <p>取得時刻: {data['timestamp']}</p>
        <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
        </body></html>
        """
    except Exception as e:
        logger.error(f"オドメータ取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/debug/charge-state")
async def debug_charge_state():
    """charge_stateの全フィールドを表示"""
    from auth import get_valid_access_token
    import httpx

    token = await get_valid_access_token()
    if not token:
        return {"error": "未認証 - /auth/login からログインしてください"}

    headers = {"Authorization": f"Bearer {token}"}
    api_base = os.getenv("TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com")

    async with httpx.AsyncClient(timeout=30.0) as client:
        v_resp = await client.get(f"{api_base}/api/1/vehicles", headers=headers)
        vehicles = v_resp.json().get("response", [])
        if not vehicles:
            return {"error": "車両なし"}

        vid = vehicles[0]["id"]
        r = await client.get(
            f"{api_base}/api/1/vehicles/{vid}/vehicle_data",
            headers=headers,
            params={"endpoints": "charge_state"},
        )
        return r.json()


# ─── Tesla パートナー登録 ───────────────────────────────────────


@app.get("/auth/register-partner", response_class=HTMLResponse)
async def register_partner():
    """Tesla Fleet API パートナーアカウントを登録（初回1回のみ実行）"""
    import httpx
    api_base = os.getenv("TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com")
    domain = "tesla-data-collector-513607963402.asia-northeast1.run.app"
    client_id = os.getenv("TESLA_CLIENT_ID")
    client_secret = os.getenv("TESLA_CLIENT_SECRET")

    # M2M (client_credentials) トークンを取得
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://auth.tesla.com/oauth2/v3/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "openid vehicle_device_data vehicle_charging_cmds",
                "audience": api_base,
            },
        )
        if token_resp.status_code != 200:
            return f"<html><body style='background:#1a1a1a;color:#fff;padding:40px;'><h2>❌ M2Mトークン取得失敗</h2><pre>{token_resp.text}</pre></body></html>"

        m2m_token = token_resp.json()["access_token"]

        response = await client.post(
            f"{api_base}/api/1/partner_accounts",
            headers={"Authorization": f"Bearer {m2m_token}", "Content-Type": "application/json"},
            json={"domain": domain},
        )

    if response.status_code in (200, 201):
        result = "✅ パートナー登録成功！"
        detail = response.text
    else:
        result = f"❌ 登録失敗 (HTTP {response.status_code})"
        detail = response.text

    return f"""
    <html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
    <h2>{result}</h2>
    <pre style="background:#2a2a2a;padding:15px;border-radius:5px;overflow:auto;">{detail}</pre>
    <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
    </body></html>
    """


# ─── Tesla 公開鍵エンドポイント ────────────────────────────────


@app.get("/.well-known/appspecific/com.tesla.3p.public-key.pem")
async def tesla_public_key():
    """Tesla Fleet API パートナー登録用の公開鍵を配信"""
    public_key = os.getenv("TESLA_PUBLIC_KEY", "")
    if not public_key:
        raise HTTPException(status_code=404, detail="公開鍵が設定されていません")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(public_key, media_type="application/x-pem-file")


# ─── 診断エンドポイント ────────────────────────────────────────


@app.get("/debug/charging")
async def debug_charging():
    """充電履歴エンドポイントの診断"""
    from auth import get_valid_access_token
    import httpx

    token = await get_valid_access_token()
    if not token:
        return {"error": "未認証 - /auth/login からログインしてください"}

    headers = {"Authorization": f"Bearer {token}"}
    api_base = os.getenv("TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com")

    results = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        # productsエンドポイント（vehicles以外の情報も含む）
        r = await client.get(f"{api_base}/api/1/products", headers=headers)
        results["products"] = {"status": r.status_code, "body": r.text[:500]}

        # vehicles一覧
        r = await client.get(f"{api_base}/api/1/vehicles", headers=headers)
        results["vehicles"] = {"status": r.status_code, "body": r.text[:500]}

        # charge_state（現在の充電状態）
        v_data = r.json().get("response", [])
        if v_data:
            vid = v_data[0].get("id")
            r2 = await client.get(
                f"{api_base}/api/1/vehicles/{vid}/vehicle_data",
                headers=headers,
                params={"endpoints": "charge_state"},
            )
            results["charge_state"] = {"status": r2.status_code, "body": r2.text[:500]}

    return results


# ─── Cloud Scheduler 用内部エンドポイント ──────────────────────


@app.post("/internal/record-odometer")
async def internal_record_odometer(x_scheduler_secret: str = Header(None)):
    """
    Cloud Scheduler から毎日 23:59 JST に呼び出されるエンドポイント
    SCHEDULER_SECRET ヘッダーで認証
    """
    if SCHEDULER_SECRET and x_scheduler_secret != SCHEDULER_SECRET:
        raise HTTPException(status_code=403, detail="認証エラー")

    if not is_authenticated():
        raise HTTPException(status_code=401, detail="Tesla未認証")

    from tesla_api import get_odometer
    from sheets import save_odometer

    try:
        data = await get_odometer()
        save_odometer(data["odometer_km"], data["vehicle_name"])
        logger.info(f"[Cloud Scheduler] オドメータ記録完了: {data['odometer_km']}km")
        return {"status": "ok", "odometer_km": data["odometer_km"]}
    except Exception as e:
        logger.error(f"[Cloud Scheduler] オドメータ取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))
