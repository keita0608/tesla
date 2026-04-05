"""
Google Sheets 連携モジュール
オドメーター・充電履歴データをスプレッドシートに書き込む
"""
import json
import os
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# シート名
SHEET_ODOMETER = "オドメータ"
SHEET_CHARGING = "充電履歴"

# オドメーターシートのヘッダー
ODOMETER_HEADERS = ["記録日", "取得時刻(JST)", "オドメータ(km)", "前日比(km)"]

# 充電履歴シートのヘッダー
CHARGING_HEADERS = [
    "日付",
    "開始時刻",
    "終了時刻",
    "充電タイプ",
    "充電場所",
    "充電場所詳細",
    "充電量(kWh)",
    "開始バッテリー(%)",
    "終了バッテリー(%)",
    "コスト",
    "通貨",
]


def _get_client() -> gspread.Client:
    """認証済みのgspreadクライアントを返す"""
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _get_or_create_sheet(spreadsheet: gspread.Spreadsheet, sheet_name: str, headers: list[str]) -> gspread.Worksheet:
    """シートを取得、なければ作成してヘッダーを設定"""
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=5000, cols=len(headers))
        worksheet.append_row(headers, value_input_option="USER_ENTERED")
    return worksheet


def save_odometer(odometer_km: float, vehicle_name: str) -> None:
    """
    オドメーターデータをスプレッドシートに保存
    前日比も自動計算して記録
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = _get_or_create_sheet(spreadsheet, SHEET_ODOMETER, ODOMETER_HEADERS)

    now_jst = datetime.now(JST)
    today = now_jst.strftime("%Y-%m-%d")
    now_time = now_jst.strftime("%H:%M:%S")
    odometer_fmt = f"{odometer_km:,.1f}"

    # 既存データから前日のオドメーターを取得して前日比を計算
    all_values = worksheet.get_all_values()
    prev_odometer = None
    daily_diff = ""

    if len(all_values) > 1:  # ヘッダー行を除く
        for row in reversed(all_values[1:]):
            if len(row) >= 3 and row[0] != today and row[2]:
                try:
                    prev_odometer = float(row[2].replace(",", ""))
                    daily_diff = round(odometer_km - prev_odometer, 1)
                    break
                except ValueError:
                    continue

    row = [today, now_time, odometer_fmt, daily_diff]
    worksheet.append_row(row, value_input_option="USER_ENTERED")
    print(f"[Sheets] オドメータ保存: {today} {odometer_km}km (前日比: {daily_diff}km)")


def save_charging_history(sessions: list[dict]) -> int:
    """
    充電履歴をスプレッドシートに保存（重複を避けて新規分のみ追記）
    戻り値: 新規追加件数
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = _get_or_create_sheet(spreadsheet, SHEET_CHARGING, CHARGING_HEADERS)

    # 既存データのキー（日付+開始時刻）を取得して重複チェック
    existing = worksheet.get_all_values()
    existing_keys = set()
    if len(existing) > 1:
        for row in existing[1:]:
            if len(row) >= 2:
                existing_keys.add(f"{row[0]}_{row[1]}")

    new_rows = []
    for session in sessions:
        key = f"{session['date']}_{session['start_time']}"
        if key in existing_keys:
            continue  # 既に記録済み

        row = [
            session["date"],
            session["start_time"],
            session["end_time"],
            session["charger_type"],
            session["location"],
            session["location_detail"],
            session["energy_added_kwh"],
            session["battery_start_pct"] if session["battery_start_pct"] is not None else "",
            session["battery_end_pct"] if session["battery_end_pct"] is not None else "",
            session["cost"] if session["cost"] is not None else "",
            session["currency"],
        ]
        new_rows.append(row)

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[Sheets] 充電履歴保存: {len(new_rows)}件追加（重複スキップ: {len(sessions) - len(new_rows)}件）")
    return len(new_rows)


def check_connection() -> bool:
    """Google Sheets への接続確認"""
    try:
        client = _get_client()
        client.open_by_key(SHEET_ID)
        return True
    except Exception as e:
        print(f"[Sheets] 接続エラー: {e}")
        return False
