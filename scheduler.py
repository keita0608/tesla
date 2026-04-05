"""
スケジューラモジュール
毎日 23:59 JST にオドメーターを自動取得してGoogle Sheetsに記録する
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _job_record_odometer() -> None:
    """23:59 に実行されるオドメーター記録ジョブ"""
    from tesla_api import get_odometer
    from sheets import save_odometer
    from auth import is_authenticated

    if not is_authenticated():
        logger.warning("[Scheduler] Tesla未認証のためオドメーター取得をスキップ")
        return

    try:
        logger.info("[Scheduler] オドメーター取得開始")
        data = await get_odometer()
        save_odometer(data["odometer_km"], data["vehicle_name"])
        logger.info(f"[Scheduler] オドメーター記録完了: {data['odometer_km']}km")
    except Exception as e:
        logger.error(f"[Scheduler] オドメーター取得エラー: {e}")


def start_scheduler() -> None:
    """スケジューラを起動"""
    # 毎日 23:59 JST (Asia/Tokyo) に実行
    scheduler.add_job(
        _job_record_odometer,
        trigger=CronTrigger(hour=23, minute=59, timezone="Asia/Tokyo"),
        id="daily_odometer",
        name="Daily Odometer Record",
        replace_existing=True,
        misfire_grace_time=60,  # 60秒以内なら遅延実行を許容
    )
    scheduler.start()
    logger.info("[Scheduler] 起動完了 - 毎日 23:59 JST にオドメーターを記録します")


def stop_scheduler() -> None:
    """スケジューラを停止"""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("[Scheduler] 停止しました")
