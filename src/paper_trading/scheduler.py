"""
调度器 — 每日定时运行实盘模拟
==============================
使用 cron 或 schedule 库定时触发。

模式:
  1. once:    运行一次 (python -m paper_trading run)
  2. daily:   每日定时 (cron: 0 15 * * 1-5)
  3. daemon:  守护进程，内置定时循环

用法:
  paper-trading run --strategy strategies/my_strategy.py
  paper-trading daemon --strategy strategies/my_strategy.py
"""

import time
import logging
import subprocess
from datetime import datetime, timedelta, time as dtime

logger = logging.getLogger(__name__)


# A 股交易时间
TRADING_START = dtime(9, 30)
TRADING_END = dtime(15, 0)
LUNCH_START = dtime(11, 30)
LUNCH_END = dtime(13, 0)


def is_trading_day(d: datetime = None) -> bool:
    """判断是否为交易日（简化：周一到周五）"""
    if d is None:
        d = datetime.now()
    return d.weekday() < 5


def is_trading_time(dt: datetime = None) -> bool:
    """判断当前是否在交易时间内"""
    if dt is None:
        dt = datetime.now()
    if not is_trading_day(dt):
        return False
    t = dt.time()
    if t < TRADING_START or t > TRADING_END:
        return False
    if LUNCH_START < t < LUNCH_END:
        return False
    return True


def run_daily(strategy_file: str, **engine_kwargs):
    """
    每日运行入口——在交易日 15:00 后触发。

    通常配置为 cron: 0 15 * * 1-5
    """
    if not is_trading_day():
        print("[Scheduler] 非交易日，跳过")
        return

    today = datetime.now().strftime('%Y%m%d')
    print(f"[Scheduler] 运行日期: {today}")

    from paper_trading.engine import PaperEngine
    engine = PaperEngine(strategy_file=strategy_file, **engine_kwargs)
    report = engine.run(trade_date=today)
    engine.print_summary(report)
    return report


def run_daemon(strategy_file: str, poll_interval: int = 60, **engine_kwargs):
    """
    守护进程模式——持续运行，每个交易日自动触发。

    轮询间隔 poll_interval 秒（默认 60s）。交易时间外休眠。
    """
    print(f"[Daemon] 启动守护进程，策略: {strategy_file}")
    last_run_date = None

    while True:
        now = datetime.now()
        today_str = now.strftime('%Y%m%d')

        # 交易日 15:00 后运行一次
        if is_trading_day(now) and now.time() > TRADING_END and last_run_date != today_str:
            print(f"[Daemon] 触发日度结算: {today_str}")
            try:
                from paper_trading.engine import PaperEngine
                engine = PaperEngine(strategy_file=strategy_file, **engine_kwargs)
                engine.run(trade_date=today_str)
                last_run_date = today_str
            except Exception as e:
                logger.error(f"[Daemon] 运行异常: {e}")

        time.sleep(poll_interval)
