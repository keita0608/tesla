"""
Tesla Fleet API クライアント
充電履歴・オドメーター情報を取得する
"""
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from auth import get_valid_access_token

load_dotenv()

API_BASE_URL = os.getenv("TESLA_API_BASE_URL", "https://fleet-api.prd.na.vn.cloud.tesla.com")


async def _get_headers() -> dict:
    token = await get_valid_access_token()
    if not token:
        raise RuntimeError("Tesla認証トークンがありません。/auth/login から認証してください。")
    return {"Authorization": f"Bearer {token}"}


async def get_vehicles() -> list[dict]:
    """所有車両一覧を取得"""
    headers = await _get_headers()
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{API_BASE_URL}/api/1/vehicles", headers=headers)
        response.raise_for_status()
        return response.json().get("response", [])


async def get_vehicle_id() -> str:
    """最初の車両IDを取得"""
    vehicles = await get_vehicles()
    if not vehicles:
        raise RuntimeError("車両が見つかりません。")
    return vehicles[0]["id"]


async def get_odometer() -> dict:
    """
    現在のオドメーター（走行距離）を取得
    戻り値: {"odometer_km": float, "timestamp": str, "vehicle_name": str}
    """
    headers = await _get_headers()
    vehicle_id = await get_vehicle_id()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 車両を起こしてからデータ取得
        response = await client.get(
            f"{API_BASE_URL}/api/1/vehicles/{vehicle_id}/vehicle_data",
            headers=headers,
            params={"endpoints": "vehicle_state"},
        )
        response.raise_for_status()
        data = response.json().get("response", {})

    vehicle_state = data.get("vehicle_state", {})
    # Tesla APIはオドメーターをマイルで返すのでkmに変換
    odometer_miles = vehicle_state.get("odometer", 0)
    odometer_km = round(odometer_miles * 1.60934, 1)

    return {
        "odometer_km": odometer_km,
        "odometer_miles": odometer_miles,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vehicle_name": data.get("display_name", "My Tesla"),
        "vin": data.get("vin", ""),
    }


async def get_charging_history() -> list[dict]:
    """
    充電履歴を取得（スーパーチャージャーのコスト含む）
    戻り値: 充電セッションのリスト
    """
    headers = await _get_headers()
    vin = await _get_vin()

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{API_BASE_URL}/api/1/vehicles/{vin}/charging_history",
            headers=headers,
        )
        response.raise_for_status()
        sessions = response.json().get("response", [])

    return [_parse_charging_session(s) for s in sessions]


async def _get_vin() -> str:
    """VINを取得"""
    vehicles = await get_vehicles()
    if not vehicles:
        raise RuntimeError("車両が見つかりません。")
    return vehicles[0]["vin"]


def _parse_charging_session(session: dict) -> dict:
    """充電セッションデータを整形"""
    # 充電タイプを判定
    charger_type = session.get("charger_type", "")
    is_supercharger = "supercharger" in charger_type.lower() if charger_type else False

    # 開始・終了時刻
    charge_start_date = session.get("charge_start_date", "")
    charge_stop_date = session.get("charge_stop_date", "")

    # 日時をJSTに変換（UTCから+9時間）
    start_jst = _to_jst(charge_start_date)
    stop_jst = _to_jst(charge_stop_date)

    # 充電量（kWh）
    energy_added_kwh = session.get("energy_added", 0)

    # コスト（スーパーチャージャーのみ取得可能）
    fees = session.get("fees", [])
    total_cost = 0.0
    currency = ""
    if fees:
        for fee in fees:
            if fee.get("fee_type") == "TOTAL":
                total_cost = fee.get("total_due", 0.0)
                currency = fee.get("currency_code", "JPY")
                break

    # 充電開始・終了時のバッテリー%
    battery_level_start = session.get("battery_level_start", None)
    battery_level_end = session.get("battery_level_end", None)

    # 充電場所
    location = session.get("site_name", session.get("charger_name", "不明"))
    address = session.get("address", {})
    if isinstance(address, dict):
        city = address.get("city", "")
        country = address.get("country", "")
        location_detail = f"{city}, {country}".strip(", ")
    else:
        location_detail = ""

    return {
        "date": start_jst[:10] if start_jst else "",
        "start_time": start_jst[11:19] if len(start_jst) >= 19 else "",
        "end_time": stop_jst[11:19] if len(stop_jst) >= 19 else "",
        "charger_type": "スーパーチャージャー" if is_supercharger else "自宅/その他",
        "location": location,
        "location_detail": location_detail,
        "energy_added_kwh": round(energy_added_kwh, 2),
        "battery_start_pct": battery_level_start,
        "battery_end_pct": battery_level_end,
        "cost": total_cost if is_supercharger else None,
        "currency": currency if is_supercharger else "",
        "raw_charger_type": charger_type,
    }


def _to_jst(iso_string: str) -> str:
    """UTC ISO文字列をJST（UTC+9）に変換して返す"""
    if not iso_string:
        return ""
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        from datetime import timedelta
        jst = dt.astimezone(timezone(timedelta(hours=9)))
        return jst.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_string
