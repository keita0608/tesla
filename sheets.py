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
SHEET_INPUT = "充電履歴_入力"

# オドメーターシートのヘッダー
ODOMETER_HEADERS = ["記録日", "取得時刻(JST)", "オドメータ(km)", "前日比(km)"]

# 充電履歴シートのヘッダー（統一フォーマット）
CHARGING_HEADERS = [
    "開始日時(JST)",
    "終了日時(JST)",
    "充電時間(分)",
    "充電量(kWh)",
    "充電タイプ",
    "費用(¥)",
    "都道府県",
    "場所名",
    "プロバイダー",
]

# 入力シートのヘッダー（ユーザーが貼り付ける列）
INPUT_HEADERS = [
    "開始日時(JST)",   # A: Tesla CSV の Charge Start Time をJST変換
    "終了日時(JST)",   # B: Tesla CSV の Charge End Time をJST変換
    "充電時間(分)",    # C: Tesla CSV の Charge Duration(s)÷60
    "充電量(kWh)",     # D: Tesla CSV の Energy Added (kWh)
    "充電タイプ",      # E: Tesla CSV の Charger Type
    "費用(¥)",         # F: 手入力
    "都道府県",        # G: 手入力
    "場所名",          # H: 手入力
    "プロバイダー",    # I: 手入力
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


def setup_input_sheet() -> str:
    """
    充電履歴_入力シートを作成（なければ）
    ユーザーがデータを貼り付けるためのテンプレートシート
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = _get_or_create_sheet(spreadsheet, SHEET_INPUT, INPUT_HEADERS)

    # ヘッダー行に背景色を設定（視認性向上）
    try:
        worksheet.format("A1:I1", {
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.2},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
        # 手入力列（F〜I）に色付け
        worksheet.format("F1:I1", {
            "backgroundColor": {"red": 0.6, "green": 0.8, "blue": 0.4},
            "textFormat": {"bold": True, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
        })
    except Exception:
        pass  # 書式設定は失敗しても続行

    return f"「{SHEET_INPUT}」シートを準備しました"


def import_from_input_sheet() -> dict:
    """
    充電履歴_入力シートから充電履歴シートへデータを取り込む
    重複チェック付き（開始日時で判定）
    戻り値: {"imported": int, "skipped": int, "errors": list}
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)

    # 入力シートを取得
    try:
        input_ws = spreadsheet.worksheet(SHEET_INPUT)
    except gspread.WorksheetNotFound:
        return {"imported": 0, "skipped": 0, "errors": ["「充電履歴_入力」シートが見つかりません。先にシートを準備してください。"]}

    # 充電履歴シートを取得（なければ作成）
    charging_ws = _get_or_create_sheet(spreadsheet, SHEET_CHARGING, CHARGING_HEADERS)

    # 入力データを取得（ヘッダー行を除く）
    input_rows = input_ws.get_all_values()
    if len(input_rows) <= 1:
        return {"imported": 0, "skipped": 0, "errors": ["入力シートにデータがありません。"]}

    # 既存データのキー（開始日時）を取得して重複チェック
    existing_rows = charging_ws.get_all_values()
    existing_keys = set()
    if len(existing_rows) > 1:
        for row in existing_rows[1:]:
            if row and row[0]:
                existing_keys.add(row[0].strip())

    new_rows = []
    errors = []

    for i, row in enumerate(input_rows[1:], start=2):  # 2行目から（1行目はヘッダー）
        if not any(row):  # 空行はスキップ
            continue

        # 開始日時（必須）
        start_time = row[0].strip() if len(row) > 0 else ""
        if not start_time:
            continue

        # 重複チェック
        if start_time in existing_keys:
            continue

        # データを整形
        try:
            new_row = [
                start_time,                                          # 開始日時(JST)
                row[1].strip() if len(row) > 1 else "",             # 終了日時(JST)
                row[2].strip() if len(row) > 2 else "",             # 充電時間(分)
                row[3].strip() if len(row) > 3 else "",             # 充電量(kWh)
                row[4].strip() if len(row) > 4 else "",             # 充電タイプ
                row[5].strip() if len(row) > 5 else "",             # 費用(¥)
                row[6].strip() if len(row) > 6 else "",             # 都道府県
                row[7].strip() if len(row) > 7 else "",             # 場所名
                row[8].strip() if len(row) > 8 else "",             # プロバイダー
            ]
            new_rows.append(new_row)
        except Exception as e:
            errors.append(f"行{i}: {e}")

    if new_rows:
        charging_ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    skipped = len(input_rows) - 1 - len(new_rows) - len(errors)
    print(f"[Sheets] インポート完了: {len(new_rows)}件追加, {skipped}件スキップ")
    return {"imported": len(new_rows), "skipped": max(skipped, 0), "errors": errors}


def save_odometer(odometer_km: float, vehicle_name: str) -> None:
    """オドメーターデータをスプレッドシートに保存"""
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = _get_or_create_sheet(spreadsheet, SHEET_ODOMETER, ODOMETER_HEADERS)

    now_jst = datetime.now(JST)
    today = now_jst.strftime("%Y-%m-%d")
    now_time = now_jst.strftime("%H:%M:%S")
    odometer_fmt = f"{odometer_km:,.1f}"

    all_values = worksheet.get_all_values()
    daily_diff = ""
    if len(all_values) > 1:
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
    """Tesla APIからの充電履歴を保存（後方互換用）"""
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)
    worksheet = _get_or_create_sheet(spreadsheet, SHEET_CHARGING, CHARGING_HEADERS)

    existing = worksheet.get_all_values()
    existing_keys = set()
    if len(existing) > 1:
        for row in existing[1:]:
            if row and row[0]:
                existing_keys.add(row[0].strip())

    new_rows = []
    for session in sessions:
        key = f"{session.get('date', '')} {session.get('start_time', '')}".strip()
        if key in existing_keys:
            continue
        row = [
            f"{session.get('date', '')} {session.get('start_time', '')}".strip(),
            f"{session.get('date', '')} {session.get('end_time', '')}".strip(),
            "",
            session.get("energy_added_kwh", ""),
            session.get("charger_type", ""),
            session.get("cost", ""),
            "",
            session.get("location", ""),
            "",
        ]
        new_rows.append(row)

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")

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
