"""
スケジューラ情報
Cloud Run 環境では Google Cloud Scheduler が
毎日 23:59 JST に POST /internal/record-odometer を呼び出す。

ローカル開発時のみ APScheduler を使用する。
"""
import logging
import os

logger = logging.getLogger(__name__)

# Cloud Run 環境かどうかを判定（Cloud Run では K_SERVICE 環境変数が設定される）
IS_CLOUD_RUN = os.getenv("K_SERVICE") is not None


def start_scheduler() -> None:
    if IS_CLOUD_RUN:
        logger.info("[Scheduler] Cloud Run 環境: Google Cloud Scheduler が 23:59 JST に自動実行します")
        return

    # ローカル開発時のみ APScheduler を起動
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        scheduler = AsyncIOScheduler()

        async def _job():
            from tesla_api import get_odometer
            from sheets import save_odometer
            from auth import is_authenticated
            if not is_authenticated():
                logger.warning("[Scheduler] Tesla未認証のためスキップ")
                return
            try:
                data = await get_odometer()
                save_odometer(data["odometer_km"], data["vehicle_name"])
                logger.info(f"[Scheduler] オドメータ記録完了: {data['odometer_km']}km")
            except Exception as e:
                logger.error(f"[Scheduler] エラー: {e}")

        scheduler.add_job(
            _job,
            trigger=CronTrigger(hour=23, minute=59, timezone="Asia/Tokyo"),
            id="daily_odometer",
            replace_existing=True,
        )
        scheduler.start()
        logger.info("[Scheduler] ローカル用スケジューラ起動: 毎日 23:59 JST")
    except ImportError:
        logger.warning("[Scheduler] APScheduler 未インストール。ローカルスケジューラは無効。")


def stop_scheduler() -> None:
    pass
