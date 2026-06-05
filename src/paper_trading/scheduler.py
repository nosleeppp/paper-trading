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

交易日历:
  通过环境变量 PAPER_TRADE_CALENDAR 指定文件路径，或代码中调用 set_trade_calendar_file()。
  文件格式: 每行一个日期，YYYY-MM-DD 或 YYYYMMDD，支持 # 注释行。
"""

import os
import time
import logging
from datetime import datetime, date, time as dtime
from typing import Optional, Set

logger = logging.getLogger(__name__)


# A 股交易时间
TRADING_START = dtime(9, 30)
TRADING_END = dtime(15, 0)
LUNCH_START = dtime(11, 30)
LUNCH_END = dtime(13, 0)


# ── 交易日历 ────────────────────────────────────────────────
_trade_calendar_file: Optional[str] = None
_trade_calendar_cache: Optional[Set[date]] = None


def set_trade_calendar_file(filepath: str) -> None:
    """
    设置交易日历文件路径。调用后立即生效，is_trading_day 会从该文件读取。
    文件格式: 每行一个日期 YYYY-MM-DD 或 YYYYMMDD，空行和 # 开头的注释行自动跳过。
    """
    global _trade_calendar_file, _trade_calendar_cache
    _trade_calendar_file = filepath
    _trade_calendar_cache = None
    logger.info("交易日历文件已设置: %s", filepath)


def _load_trade_calendar_from_file() -> Optional[Set[date]]:
    """从文件加载交易日历，成功返回 set 否则 None。"""
    global _trade_calendar_cache, _trade_calendar_file

    if _trade_calendar_cache is not None:
        return _trade_calendar_cache

    # 优先代码设置的路径，其次环境变量
    if _trade_calendar_file is None:
        env_file = os.environ.get('PAPER_TRADE_CALENDAR', '')
        if env_file:
            _trade_calendar_file = env_file

    if _trade_calendar_file is None:
        return None

    if not os.path.exists(_trade_calendar_file):
        logger.warning("交易日历文件不存在: %s", _trade_calendar_file)
        return None

    dates = set()
    try:
        ext = os.path.splitext(_trade_calendar_file)[1].lower()

        if ext in ('.parquet', '.pq'):
            # Parquet 格式 → pandas 读取
            import pandas as pd
            df = pd.read_parquet(_trade_calendar_file)
            # 常见的日期列名
            date_col = None
            for col in ('trade_date', 'cal_date', 'date', 'calendar_date', 'trade_dt'):
                if col in df.columns:
                    date_col = col
                    break
            if date_col is None:
                date_col = df.columns[0]
            for val in df[date_col]:
                if hasattr(val, 'date'):
                    dates.add(val.date())
                elif hasattr(val, 'strftime'):
                    dates.add(val)
                else:
                    s = str(val)[:10]
                    clean = s.replace('-', '')
                    if len(clean) == 8 and clean.isdigit():
                        dates.add(date(int(clean[:4]), int(clean[4:6]), int(clean[6:8])))
        else:
            # 文本格式：每行一个日期 YYYY-MM-DD 或 YYYYMMDD
            with open(_trade_calendar_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    clean = line.replace('-', '')
                    if len(clean) == 8 and clean.isdigit():
                        dates.add(date(int(clean[:4]), int(clean[4:6]), int(clean[6:8])))

        _trade_calendar_cache = dates
        logger.info("交易日历已加载: %d 天 (来源: %s)", len(dates), _trade_calendar_file)
        return dates
    except Exception:
        logger.warning("交易日历文件读取失败: %s", _trade_calendar_file, exc_info=True)
        return None


def _get_trade_calendar() -> Optional[Set[date]]:
    """获取交易日历 — 优先参数 > 文件 > RuntimeError。"""
    calendar = _load_trade_calendar_from_file()
    if calendar is not None:
        return calendar
    return None


def is_trading_day(
    d: Optional[datetime] = None,
    trade_calendar: Optional[Set[date]] = None,
) -> bool:
    """
    判断是否为 A 股交易日。

    优先级:
    1. 参数 trade_calendar — 调用方直接传入的 date 集合
    2. 文件 — set_trade_calendar_file() 或环境变量 PAPER_TRADE_CALENDAR 指定的文件
    3. 无可用数据源 — 抛出 RuntimeError
    """
    if d is None:
        d = datetime.now()

    check_date = d.date() if isinstance(d, datetime) else d

    # 1. 参数提供的交易日历
    if trade_calendar is not None:
        return check_date in trade_calendar

    # 2. 文件加载的交易日历
    calendar = _get_trade_calendar()
    if calendar is not None:
        return check_date in calendar

    # 3. 无可用数据源
    raise RuntimeError(
        "无法判断交易日：未提供 trade_calendar 参数，且未设置交易日历文件。\n"
        "请通过 set_trade_calendar_file('/path/to/calendar.txt') 或\n"
        "环境变量 PAPER_TRADE_CALENDAR 指定交易日历文件路径。\n"
        "文件格式: 每行一个日期 YYYY-MM-DD 或 YYYYMMDD。"
    )


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


def run_daily(strategy_file: str, trade_calendar: Set[date] = None, **engine_kwargs):
    """
    每日运行入口——在交易日 15:00 后触发。

    通常配置为 cron: 0 15 * * 1-5
    """
    if not is_trading_day(trade_calendar=trade_calendar):
        print("[Scheduler] 非交易日，跳过")
        return

    today = datetime.now().strftime('%Y%m%d')
    print(f"[Scheduler] 运行日期: {today}")

    from paper_trading.engine import PaperEngine
    engine = PaperEngine(strategy_file=strategy_file, **engine_kwargs)
    report = engine.run(trade_date=today)
    engine.print_summary(report)
    return report


def run_daemon(strategy_file: str, poll_interval: int = 60,
               trade_calendar: Set[date] = None, **engine_kwargs):
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
        if is_trading_day(now, trade_calendar=trade_calendar) \
                and now.time() > TRADING_END \
                and last_run_date != today_str:
            print(f"[Daemon] 触发日度结算: {today_str}")
            try:
                from paper_trading.engine import PaperEngine
                engine = PaperEngine(strategy_file=strategy_file, **engine_kwargs)
                engine.run(trade_date=today_str)
                last_run_date = today_str
            except Exception as e:
                logger.error(f"[Daemon] 运行异常: {e}")

        time.sleep(poll_interval)
