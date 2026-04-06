"""
Tesla データ収集 Web アプリ
FastAPI + Tesla Fleet API + Google Sheets
Cloud Run / Cloud Scheduler 対応
Google OAuth によるアクセス制限付き
"""
import logging
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Header
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

from auth import (
    generate_pkce_pair,
    get_authorization_url,
    exchange_code_for_tokens,
    is_authenticated,
    load_tokens,
)
from scheduler import start_scheduler, stop_scheduler

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Google OAuth 設定
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")
ALLOWED_EMAIL = os.getenv("ALLOWED_EMAIL")

# Cloud Scheduler シークレット
SCHEDULER_SECRET = os.getenv("SCHEDULER_SECRET", "")

# PKCE用一時データ
_pkce_store: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Tesla Data Collector", lifespan=lifespan)

# 静的ファイル配信
app.mount("/static", StaticFiles(directory="static"), name="static")

# セッションミドルウェア（Google OAuth用）
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("APP_SECRET_KEY", "change-me"),
    max_age=86400 * 30,  # 30日間セッション維持
)


# ─── Google OAuth 認証ヘルパー ─────────────────────────────────


def get_login_user(request: Request) -> str | None:
    """セッションからログイン中のユーザーメールを取得"""
    return request.session.get("user_email")


def require_login(request: Request) -> str:
    """ログイン必須の依存関係 - 未ログインはログイン画面へリダイレクト"""
    email = request.session.get("user_email")
    if not email:
        raise HTTPException(
            status_code=307,
            headers={"Location": "/login"},
        )
    return email


# ─── Google OAuth エンドポイント ───────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Googleログイン画面へリダイレクト"""
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
async def google_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    error: str = Query(None),
):
    """Google OAuthコールバック処理"""
    if error:
        return HTMLResponse(f"<h2>ログインエラー: {error}</h2>", status_code=400)

    if state != request.session.get("oauth_state"):
        return HTMLResponse("<h2>不正なアクセスです。再度ログインしてください。</h2>", status_code=400)

    async with httpx.AsyncClient() as client:
        # コードをトークンに交換
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        token_data = token_resp.json()

        # ユーザー情報取得
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        userinfo = userinfo_resp.json()

    email = userinfo.get("email", "")

    # 許可されたメールアドレスのみアクセス可能
    if email != ALLOWED_EMAIL:
        logger.warning(f"[Auth] 不正アクセス試行: {email}")
        return HTMLResponse(
            f"""<html><body style="font-family:sans-serif;padding:40px;">
            <h2>アクセス権限がありません</h2>
            <p>{email} はこのアプリへのアクセス権限がありません。</p>
            </body></html>""",
            status_code=403,
        )

    request.session["user_email"] = email
    request.session["user_name"] = userinfo.get("name", "")
    logger.info(f"[Auth] ログイン成功: {email}")
    return RedirectResponse("/")


@app.get("/logout")
async def logout(request: Request):
    """ログアウト"""
    request.session.clear()
    return RedirectResponse("/login")


# ─── ステータスページ ────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(require_login)):
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
        <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
        <meta name="mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-capable" content="yes">
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
        <meta name="apple-mobile-web-app-title" content="Tesla DC">
        <meta name="theme-color" content="#e82127">
        <link rel="manifest" href="/static/manifest.json">
        <link rel="apple-touch-icon" href="/static/icon-192.png">
        <title>Tesla Data Collector</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: env(safe-area-inset-top, 20px) 16px 40px; background: #1a1a1a; color: #fff; }}
            h1 {{ color: #e82127; font-size: 1.6em; margin-bottom: 4px; }}
            h2 {{ font-size: 1.1em; margin: 0 0 12px; }}
            .card {{ background: #2a2a2a; border-radius: 12px; padding: 16px; margin: 12px 0; }}
            .status {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 0.9em; }}
            .btn {{ display: inline-block; background: #444; color: white; padding: 10px 18px; border-radius: 8px; text-decoration: none; margin: 4px; font-size: 0.9em; }}
            .btn:active {{ opacity: 0.7; }}
            .btn-primary {{ background: #e82127; }}
            .btn-full {{ display: block; text-align: center; width: 100%; }}
            .user-info {{ text-align: right; font-size: 0.8em; color: #aaa; margin-bottom: 8px; padding-top: 8px; }}
        </style>
    </head>
    <body>
        <div class="user-info">👤 {user} | <a href="/logout" style="color:#e82127;">ログアウト</a></div>
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
            <a href="/charging/sync" class="btn btn-primary">🔋 充電履歴を同期</a>
            <a href="/charging/history" class="btn">📋 充電履歴（JSON）</a>
        </div>

        <div class="card">
            <h2>📥 充電履歴インポート</h2>
            <p>「充電履歴_入力」シートにデータを貼り付けてから取り込みます。</p>
            <a href="/charging/setup-input-sheet" class="btn">🗂️ 入力シートを準備</a>
            <a href="/charging/import" class="btn btn-primary">📤 入力シートから取込</a>
        </div>

        <div class="card">
            <h2>オドメータ</h2>
            <p>※ 毎日 23:59 JST に Cloud Scheduler が自動記録します。</p>
            <a href="/odometer/now" class="btn btn-primary">🚗 今すぐオドメータを記録</a>
            <a href="/odometer/current" class="btn">📊 現在値（JSON）</a>
        </div>

        <div class="card">
            <h2>📊 集計レポート</h2>
            <p>充電履歴から月次集計と充電場所ランキングを自動生成します。</p>
            <a href="/summary/update" class="btn btn-primary">🔄 集計を更新</a>
        </div>
    </body>
    </html>
    """


# ─── Tesla 認証 ────────────────────────────────────────────────


@app.get("/auth/login", dependencies=[Depends(require_login)])
async def auth_login():
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
    if error:
        raise HTTPException(status_code=400, detail=f"認証エラー: {error}")
    code_verifier = _pkce_store.pop(state, None)
    if not code_verifier:
        raise HTTPException(status_code=400, detail="不正なstateパラメータです。")
    try:
        await exchange_code_for_tokens(code, code_verifier)
        return RedirectResponse("/?auth=success")
    except Exception as e:
        logger.error(f"トークン取得エラー: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ─── 充電履歴 ──────────────────────────────────────────────────


@app.get("/charging/history", dependencies=[Depends(require_login)])
async def charging_history():
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="Tesla未認証")
    from tesla_api import get_charging_history
    sessions = await get_charging_history()
    return {"count": len(sessions), "sessions": sessions}


@app.get("/charging/sync", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def charging_sync():
    if not is_authenticated():
        return RedirectResponse("/auth/login")
    from tesla_api import get_charging_history
    from sheets import save_charging_history
    try:
        sessions = await get_charging_history()
        added = save_charging_history(sessions)
        return f"""<html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ 充電履歴同期完了</h2>
        <p>取得件数: {len(sessions)}件 / 新規追加: {added}件</p>
        <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
        </body></html>"""
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── 充電履歴インポート ────────────────────────────────────────


@app.get("/charging/setup-input-sheet", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def setup_input_sheet():
    """充電履歴_入力シートを準備"""
    from sheets import setup_input_sheet as _setup
    try:
        msg = _setup()
        return f"""<html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ {msg}</h2>
        <h3>入力方法</h3>
        <ol style="line-height:2;">
            <li>Teslaサイトから充電データCSVをダウンロード</li>
            <li>スプレッドシートの「充電履歴_入力」シートを開く</li>
            <li>CSVの内容を貼り付け（開始日時はJSTに変換）</li>
            <li>費用・都道府県・場所名・プロバイダーを手入力</li>
            <li>アプリに戻り「入力シートから取込」をクリック</li>
        </ol>
        <h3>列の対応（Tesla公式CSV → 入力シート）</h3>
        <table style="border-collapse:collapse;width:100%;">
            <tr style="background:#333;"><th style="padding:8px;border:1px solid #555;">入力シート列</th><th style="padding:8px;border:1px solid #555;">Tesla CSV列</th><th style="padding:8px;border:1px solid #555;">備考</th></tr>
            <tr><td style="padding:8px;border:1px solid #555;">開始日時(JST)</td><td style="padding:8px;border:1px solid #555;">Charge Start Time (UTC)</td><td style="padding:8px;border:1px solid #555;">UTC+9時間に変換</td></tr>
            <tr><td style="padding:8px;border:1px solid #555;">終了日時(JST)</td><td style="padding:8px;border:1px solid #555;">Charge End Time (UTC)</td><td style="padding:8px;border:1px solid #555;">UTC+9時間に変換</td></tr>
            <tr><td style="padding:8px;border:1px solid #555;">充電時間(分)</td><td style="padding:8px;border:1px solid #555;">Charge Duration (s)</td><td style="padding:8px;border:1px solid #555;">÷60で分に変換</td></tr>
            <tr><td style="padding:8px;border:1px solid #555;">充電量(kWh)</td><td style="padding:8px;border:1px solid #555;">Energy Added (kWh)</td><td style="padding:8px;border:1px solid #555;">そのまま</td></tr>
            <tr><td style="padding:8px;border:1px solid #555;">充電タイプ</td><td style="padding:8px;border:1px solid #555;">Charger Type</td><td style="padding:8px;border:1px solid #555;">そのまま</td></tr>
            <tr style="background:#2a3a2a;"><td style="padding:8px;border:1px solid #555;">費用(¥)</td><td style="padding:8px;border:1px solid #555;">-</td><td style="padding:8px;border:1px solid #555;">手入力（緑色列）</td></tr>
            <tr style="background:#2a3a2a;"><td style="padding:8px;border:1px solid #555;">都道府県</td><td style="padding:8px;border:1px solid #555;">-</td><td style="padding:8px;border:1px solid #555;">手入力（緑色列）</td></tr>
            <tr style="background:#2a3a2a;"><td style="padding:8px;border:1px solid #555;">場所名</td><td style="padding:8px;border:1px solid #555;">-</td><td style="padding:8px;border:1px solid #555;">手入力（緑色列）</td></tr>
            <tr style="background:#2a3a2a;"><td style="padding:8px;border:1px solid #555;">プロバイダー</td><td style="padding:8px;border:1px solid #555;">-</td><td style="padding:8px;border:1px solid #555;">手入力（緑色列）</td></tr>
        </table>
        <br><a href="/" style="color:#e82127;">← トップに戻る</a>
        </body></html>"""
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/charging/import", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def charging_import():
    """充電履歴_入力シートから充電履歴シートへ取り込む"""
    from sheets import import_from_input_sheet
    try:
        result = import_from_input_sheet()
        errors_html = ""
        if result["errors"]:
            errors_html = "<h3>⚠️ エラー</h3><ul>" + "".join(f"<li>{e}</li>" for e in result["errors"]) + "</ul>"
        return f"""<html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ インポート完了</h2>
        <p>新規取込: <strong>{result['imported']}件</strong></p>
        <p>重複スキップ: {result['skipped']}件</p>
        {errors_html}
        <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
        </body></html>"""
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── オドメータ ────────────────────────────────────────────────


@app.get("/odometer/current", dependencies=[Depends(require_login)])
async def odometer_current():
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="Tesla未認証")
    from tesla_api import get_odometer
    return await get_odometer()


@app.get("/odometer/now", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def odometer_now():
    if not is_authenticated():
        return RedirectResponse("/auth/login")
    from tesla_api import get_odometer
    from sheets import save_odometer
    try:
        data = await get_odometer()
        save_odometer(data["odometer_km"], data["vehicle_name"])
        return f"""<html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ オドメータ記録完了</h2>
        <p>オドメータ: {data['odometer_km']:,} km</p>
        <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
        </body></html>"""
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Cloud Scheduler 用内部エンドポイント ──────────────────────


@app.post("/internal/record-odometer")
async def internal_record_odometer(x_scheduler_secret: str = Header(None)):
    if SCHEDULER_SECRET and x_scheduler_secret != SCHEDULER_SECRET:
        raise HTTPException(status_code=403, detail="認証エラー")
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="Tesla未認証")
    from tesla_api import get_odometer
    from sheets import save_odometer
    try:
        data = await get_odometer()
        save_odometer(data["odometer_km"], data["vehicle_name"])
        return {"status": "ok", "odometer_km": data["odometer_km"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Tesla 公開鍵・パートナー登録 ──────────────────────────────


@app.get("/.well-known/appspecific/com.tesla.3p.public-key.pem")
async def tesla_public_key():
    public_key = os.getenv("TESLA_PUBLIC_KEY", "")
    if not public_key:
        raise HTTPException(status_code=404, detail="公開鍵が設定されていません")
    return PlainTextResponse(public_key, media_type="application/x-pem-file")


@app.get("/auth/register-partner", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def register_partner():
    api_base = os.getenv("TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com")
    domain = "tesla-data-collector-513607963402.asia-northeast1.run.app"
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://auth.tesla.com/oauth2/v3/token",
            data={
                "grant_type": "client_credentials",
                "client_id": os.getenv("TESLA_CLIENT_ID"),
                "client_secret": os.getenv("TESLA_CLIENT_SECRET"),
                "scope": "openid vehicle_device_data vehicle_charging_cmds",
                "audience": api_base,
            },
        )
        m2m_token = token_resp.json().get("access_token", "")
        response = await client.post(
            f"{api_base}/api/1/partner_accounts",
            headers={"Authorization": f"Bearer {m2m_token}", "Content-Type": "application/json"},
            json={"domain": domain},
        )
    result = "✅ パートナー登録成功！" if response.status_code in (200, 201) else f"❌ 登録失敗 (HTTP {response.status_code})"
    return f"""<html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
    <h2>{result}</h2><pre style="background:#2a2a2a;padding:15px;border-radius:5px;">{response.text}</pre>
    <p><a href="/" style="color:#e82127;">← トップに戻る</a></p></body></html>"""


# ─── 集計レポート ──────────────────────────────────────────────


@app.get("/summary/update", response_class=HTMLResponse, dependencies=[Depends(require_login)])
async def summary_update():
    """月次集計・充電場所ランキングを更新"""
    from sheets import calculate_monthly_summary, calculate_location_ranking
    try:
        monthly = calculate_monthly_summary()
        location = calculate_location_ranking()
        monthly_err = f'<p style="color:#f88">⚠️ {monthly.get("error")}</p>' if monthly.get("error") else ""
        location_err = f'<p style="color:#f88">⚠️ {location.get("error")}</p>' if location.get("error") else ""
        return f"""<html><body style="font-family:sans-serif;background:#1a1a1a;color:#fff;padding:40px;">
        <h2>✅ 集計更新完了</h2>
        <h3>月次集計</h3>
        <p>更新: <strong>{monthly.get('months', 0)}ヶ月分</strong></p>
        {monthly_err}
        <h3>充電場所ランキング</h3>
        <p>更新: <strong>{location.get('locations', 0)}件</strong></p>
        {location_err}
        <p><a href="/" style="color:#e82127;">← トップに戻る</a></p>
        </body></html>"""
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── デバッグ ──────────────────────────────────────────────────


@app.get("/debug/charge-state", dependencies=[Depends(require_login)])
async def debug_charge_state():
    from auth import get_valid_access_token
    token = await get_valid_access_token()
    if not token:
        return {"error": "未認証"}
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
