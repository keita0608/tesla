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

# 入力シートのヘッダー（Tesla CSV形式 + 手入力列）
INPUT_HEADERS = [
    "Charge Start Time (UTC)",   # A: Tesla CSV そのまま貼り付け
    "Charge End Time (UTC)",     # B: Tesla CSV そのまま貼り付け
    "Charge Duration (s)",       # C: Tesla CSV そのまま貼り付け
    "Energy Added (kWh)",        # D: Tesla CSV そのまま貼り付け
    "Charger Type",              # E: Tesla CSV そのまま貼り付け
    "Charge Start Time (JST)",   # F: 数式列（アプリは無視）
    "Charge End Time (JST)",     # G: 数式列（アプリは無視）
    "Minutes",                   # H: 数式列（アプリは無視）
    "Date",                      # I: 数式列（アプリは無視）
    "Price",                     # J: 手入力
    "Prefecture",                # K: 手入力
    "Location",                  # L: 手入力
    "Provider",                  # M: 手入力
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


def _utc_to_jst(utc_str: str) -> str:
    """UTC文字列をJST（+9時間）に変換して返す"""
    if not utc_str:
        return ""
    # Tesla CSVの形式: "2023/10/29 11:13" or "2023-10-29 11:13" 等
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt_utc = datetime.strptime(utc_str, fmt).replace(tzinfo=timezone.utc)
            dt_jst = dt_utc.astimezone(JST)
            return dt_jst.strftime("%Y/%m/%d %H:%M")
        except ValueError:
            continue
    return utc_str  # 変換できない場合はそのまま返す


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

        # A列: Charge Start Time (UTC) が必須
        start_utc = row[0].strip() if len(row) > 0 else ""
        if not start_utc:
            continue

        # UTC→JST変換（+9時間）
        try:
            start_jst = _utc_to_jst(start_utc)
            end_jst = _utc_to_jst(row[1].strip() if len(row) > 1 else "")
        except Exception as e:
            errors.append(f"行{i}: 日時変換エラー - {e}")
            continue

        # 重複チェック（開始日時JSTで判定）
        if start_jst in existing_keys:
            continue

        # 充電時間：秒→分に変換
        try:
            duration_sec = float(row[2].strip()) if len(row) > 2 and row[2].strip() else 0
            duration_min = round(duration_sec / 60) if duration_sec else ""
        except ValueError:
            duration_min = ""

        # データを整形（A〜EのTesla CSV + J〜Mの手入力）
        try:
            new_row = [
                start_jst,                                            # 開始日時(JST)
                end_jst,                                              # 終了日時(JST)
                duration_min,                                         # 充電時間(分)
                row[3].strip() if len(row) > 3 else "",              # 充電量(kWh)
                row[4].strip() if len(row) > 4 else "",              # 充電タイプ
                row[9].strip() if len(row) > 9 else "",              # 費用(¥) ← J列
                row[10].strip() if len(row) > 10 else "",            # 都道府県 ← K列
                row[11].strip() if len(row) > 11 else "",            # 場所名 ← L列
                row[12].strip() if len(row) > 12 else "",            # プロバイダー ← M列
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


# 月次集計シートのヘッダー
MONTHLY_HEADERS = [
    "年月",
    "無料充電量(kWh)",
    "有料充電量(kWh)",
    "費用(¥)",
    "総走行距離(km)",
    "月間走行距離(km)",
    "電費(円/km)",
    "電費(円/kWh)",
]

# 充電場所ランキングシートのヘッダー
LOCATION_HEADERS = [
    "場所名",
    "都道府県",
    "プロバイダー",
    "充電量(kWh)",
    "利用回数",
    "利用金額(¥)",
    "1kW単価(¥/kWh)",
]

SHEET_MONTHLY = "月次集計"
SHEET_LOCATION = "充電場所ランキング"


def calculate_monthly_summary() -> dict:
    """
    充電履歴・オドメータから月次集計を計算してシートに書き込む
    戻り値: {"months": int, "updated": bool}
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)

    # 充電履歴を読み込む
    try:
        charging_ws = spreadsheet.worksheet(SHEET_CHARGING)
        charging_rows = charging_ws.get_all_values()
    except gspread.WorksheetNotFound:
        return {"months": 0, "updated": False, "error": "充電履歴シートが見つかりません"}

    # オドメータを読み込む
    try:
        odo_ws = spreadsheet.worksheet(SHEET_ODOMETER)
        odo_rows = odo_ws.get_all_values()
    except gspread.WorksheetNotFound:
        odo_rows = []

    # 充電履歴を月別に集計
    # headers: 開始日時(JST), 終了日時(JST), 充電時間(分), 充電量(kWh), 充電タイプ, 費用(¥), 都道府県, 場所名, プロバイダー
    monthly_data: dict[str, dict] = {}

    for row in charging_rows[1:]:
        if not any(row) or not row[0]:
            continue
        start_jst = row[0].strip()
        # 年月を抽出（例: "2024/01/15 10:30" → "2024/01"）
        try:
            if "/" in start_jst:
                parts = start_jst.split("/")
                ym = f"{parts[0]}/{parts[1]}"
            elif "-" in start_jst:
                parts = start_jst.split("-")
                ym = f"{parts[0]}/{parts[1]}"
            else:
                continue
        except (IndexError, ValueError):
            continue

        if ym not in monthly_data:
            monthly_data[ym] = {"free_kwh": 0.0, "paid_kwh": 0.0, "cost": 0.0}

        try:
            kwh = float(row[3].replace(",", "")) if len(row) > 3 and row[3].strip() else 0.0
        except ValueError:
            kwh = 0.0

        try:
            cost = float(row[5].replace(",", "")) if len(row) > 5 and row[5].strip() else 0.0
        except ValueError:
            cost = 0.0

        charger_type = row[4].strip() if len(row) > 4 else ""
        # Supercharger = 有料、それ以外 = 無料（費用が0なら無料とみなす）
        if cost > 0:
            monthly_data[ym]["paid_kwh"] += kwh
            monthly_data[ym]["cost"] += cost
        else:
            monthly_data[ym]["free_kwh"] += kwh

    # オドメータから月末走行距離を収集
    # headers: 記録日, 取得時刻(JST), オドメータ(km), 前日比(km)
    monthly_odo: dict[str, float] = {}  # 年月 → 月末オドメータ値（最大値）
    for row in odo_rows[1:]:
        if not any(row) or not row[0]:
            continue
        date_str = row[0].strip()
        try:
            if "-" in date_str:
                parts = date_str.split("-")
                ym = f"{parts[0]}/{parts[1]}"
            elif "/" in date_str:
                parts = date_str.split("/")
                ym = f"{parts[0]}/{parts[1]}"
            else:
                continue
            odo_val = float(row[2].replace(",", "")) if len(row) > 2 and row[2].strip() else 0.0
            if ym not in monthly_odo or odo_val > monthly_odo[ym]:
                monthly_odo[ym] = odo_val
        except (IndexError, ValueError):
            continue

    # 月次集計シートを作成・更新
    monthly_ws = _get_or_create_sheet(spreadsheet, SHEET_MONTHLY, MONTHLY_HEADERS)

    # 全データを上書き（ヘッダー行を残して書き直し）
    sorted_months = sorted(monthly_data.keys())
    new_rows = []
    odo_list = sorted(monthly_odo.keys())

    for i, ym in enumerate(sorted_months):
        d = monthly_data[ym]
        free_kwh = round(d["free_kwh"], 2)
        paid_kwh = round(d["paid_kwh"], 2)
        total_kwh = free_kwh + paid_kwh
        cost = round(d["cost"])

        # 総走行距離（その月の最大オドメータ値）
        total_odo = monthly_odo.get(ym, "")
        total_odo_fmt = f"{total_odo:,.1f}" if isinstance(total_odo, float) and total_odo > 0 else ""

        # 月間走行距離（前月末との差分）
        monthly_km = ""
        if i > 0:
            prev_ym = sorted_months[i - 1]
            prev_odo = monthly_odo.get(prev_ym)
            cur_odo = monthly_odo.get(ym)
            if prev_odo and cur_odo:
                monthly_km = round(cur_odo - prev_odo, 1)
        elif odo_list:
            # 最初の月は総走行距離をそのまま使う場合もある
            pass

        # 電費計算
        cost_per_km = ""
        cost_per_kwh = ""
        if isinstance(monthly_km, (int, float)) and monthly_km > 0 and cost > 0:
            cost_per_km = round(cost / monthly_km, 1)
        if total_kwh > 0 and cost > 0:
            cost_per_kwh = round(cost / paid_kwh, 1) if paid_kwh > 0 else ""

        new_rows.append([
            ym,
            free_kwh,
            paid_kwh,
            cost,
            total_odo_fmt,
            monthly_km,
            cost_per_km,
            cost_per_kwh,
        ])

    # ヘッダー行以降を全て上書き
    # 既存データをクリアして書き直す
    all_values = monthly_ws.get_all_values()
    if len(all_values) > 1:
        # データ行を削除（ヘッダーは残す）
        monthly_ws.delete_rows(2, len(all_values))

    if new_rows:
        monthly_ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[Sheets] 月次集計更新: {len(new_rows)}ヶ月分")
    return {"months": len(new_rows), "updated": True}


def calculate_location_ranking() -> dict:
    """
    充電履歴から充電場所ランキングを計算してシートに書き込む
    戻り値: {"locations": int, "updated": bool}
    """
    client = _get_client()
    spreadsheet = client.open_by_key(SHEET_ID)

    try:
        charging_ws = spreadsheet.worksheet(SHEET_CHARGING)
        charging_rows = charging_ws.get_all_values()
    except gspread.WorksheetNotFound:
        return {"locations": 0, "updated": False, "error": "充電履歴シートが見つかりません"}

    # 場所別に集計
    # headers: 開始日時(JST), 終了日時(JST), 充電時間(分), 充電量(kWh), 充電タイプ, 費用(¥), 都道府県, 場所名, プロバイダー
    location_data: dict[str, dict] = {}

    for row in charging_rows[1:]:
        if not any(row) or not row[0]:
            continue

        location = row[7].strip() if len(row) > 7 and row[7].strip() else "不明"
        prefecture = row[6].strip() if len(row) > 6 else ""
        provider = row[8].strip() if len(row) > 8 else ""

        try:
            kwh = float(row[3].replace(",", "")) if len(row) > 3 and row[3].strip() else 0.0
        except ValueError:
            kwh = 0.0

        try:
            cost = float(row[5].replace(",", "")) if len(row) > 5 and row[5].strip() else 0.0
        except ValueError:
            cost = 0.0

        if location not in location_data:
            location_data[location] = {
                "prefecture": prefecture,
                "provider": provider,
                "kwh": 0.0,
                "count": 0,
                "cost": 0.0,
            }

        location_data[location]["kwh"] += kwh
        location_data[location]["count"] += 1
        location_data[location]["cost"] += cost
        # 都道府県・プロバイダーは最新値で上書き（空でなければ）
        if prefecture:
            location_data[location]["prefecture"] = prefecture
        if provider:
            location_data[location]["provider"] = provider

    # 充電量順にソート
    sorted_locations = sorted(location_data.items(), key=lambda x: x[1]["kwh"], reverse=True)

    location_ws = _get_or_create_sheet(spreadsheet, SHEET_LOCATION, LOCATION_HEADERS)

    new_rows = []
    for loc_name, d in sorted_locations:
        kwh = round(d["kwh"], 2)
        cost = round(d["cost"])
        count = d["count"]
        unit_price = round(cost / kwh, 1) if kwh > 0 and cost > 0 else ""
        new_rows.append([
            loc_name,
            d["prefecture"],
            d["provider"],
            kwh,
            count,
            cost,
            unit_price,
        ])

    # ヘッダー行以降を上書き
    all_values = location_ws.get_all_values()
    if len(all_values) > 1:
        location_ws.delete_rows(2, len(all_values))

    if new_rows:
        location_ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    print(f"[Sheets] 充電場所ランキング更新: {len(new_rows)}件")
    return {"locations": len(new_rows), "updated": True}


def check_connection() -> bool:
    """Google Sheets への接続確認"""
    try:
        client = _get_client()
        client.open_by_key(SHEET_ID)
        return True
    except Exception as e:
        print(f"[Sheets] 接続エラー: {e}")
        return False
