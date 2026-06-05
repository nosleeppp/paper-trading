"""
IC5_T100_ICIRTM10_ICIRN20_月频 paper_trading 适配策略
=====================================================
桥接 quant_backtest 信号生成 ↔ paper_trading 实盘模拟执行。

调仓节奏（月频）：
  - 每月倒数第 2 交易日 15:00 → 调用 quant_backtest 的 _on_select 生成信号
  - 每月最后交易日 09:30   → 从策略缓存读取目标池，等权调仓

前置条件：
  1. 服务器已安装 quant_backtest >= 0.7.18
  2. DuckDB 因子库路径 /root/lqq_bot_workspace/data/data.duckdb
  3. 原始回测策略文件在同级或上层目录可 import

用法：
  paper-trading daemon --strategy strategies/pcat_icir_100_monthly.py
"""

from __future__ import annotations
import sys
import os
import json
import logging
import calendar
from datetime import datetime, date
from typing import List, Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# 服务器配置（按需修改）
# ═══════════════════════════════════════════════════════════════════════
DATA_DIR = '/root/lqq_bot_workspace/data'
OUTPUT_DIR = '/root/lqq_bot_workspace/zz1000/output'
SIGNAL_FILE = os.path.join(OUTPUT_DIR, 'paper_signal_targets.json')

# 确保回测策略可 import
_COLLAB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _COLLAB_ROOT not in sys.path:
    sys.path.insert(0, _COLLAB_ROOT)

# ═══════════════════════════════════════════════════════════════════════
# 交易日辅助（基于 akshare 交易日历，回退 weekday 近似）
# ═══════════════════════════════════════════════════════════════════════

_trade_dates_cache: Optional[set] = None


def _load_trade_calendar() -> set:
    """加载 A 股交易日历，返回 'YYYYMMDD' 字符串集合。"""
    global _trade_dates_cache
    if _trade_dates_cache is not None:
        return _trade_dates_cache
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        _trade_dates_cache = set(
            d.strftime('%Y%m%d') for d in df['trade_date']
        )
        logger.info("交易日历已加载，共 %d 天", len(_trade_dates_cache))
    except Exception:
        logger.warning("无法加载 akshare 交易日历，回退 weekday 近似")
        _trade_dates_cache = set()  # 空集合表示回退
    return _trade_dates_cache


def _get_month_trading_days(year: int, month: int) -> List[str]:
    """
    返回指定年月的所有交易日，YYYYMMDD 字符串，按日期升序。
    """
    cal = _load_trade_calendar()
    if cal:
        # 使用真实交易日历
        days = []
        for d in range(1, 32):
            try:
                dt = date(year, month, d)
            except ValueError:
                break
            ds = dt.strftime('%Y%m%d')
            if ds in cal:
                days.append(ds)
        return days

    # 回退：所有工作日
    _, last_day = calendar.monthrange(year, month)
    days = []
    for d in range(1, last_day + 1):
        dt = date(year, month, d)
        if dt.weekday() < 5:
            days.append(dt.strftime('%Y%m%d'))
    return days


def _get_nth_last_trading_day(year: int, month: int, offset: int) -> Optional[str]:
    """
    取当月倒数第 |offset| 个交易日。
    offset=-1 → 最后一个交易日
    offset=-2 → 倒数第二个交易日
    返回 'YYYYMMDD' 或 None。
    """
    days = _get_month_trading_days(year, month)
    if not days:
        return None
    try:
        return days[offset]
    except IndexError:
        return None


# ═══════════════════════════════════════════════════════════════════════
# quant_backtest 信号桥接
# ═══════════════════════════════════════════════════════════════════════

class _MockBacktester:
    """最小 mock — 为 quant_backtest context 提供 _data_cache。"""

    def __init__(self, data_cache):
        self._data_cache = data_cache


class _MockContext:
    """最小 mock context — 为 quant_backtest 的 _on_select 提供所需属性。"""

    def __init__(self, current_dt: str, data_cache):
        self.current_dt = current_dt
        self._backtester = _MockBacktester(data_cache)


def _init_quant_strategy():
    """
    初始化 quant_backtest 策略实例 + DataCache + 因子缓存。
    返回 (strategy, data_cache) 或 (None, None)。
    """
    try:
        from quant_backtest import DataCache
        # 动态导入用户策略类（文件名带 TM10_ICIRN20）
        from IC5_T100_ICIRTM10_ICIRN20_月频_218因子 import ICIR_Screen_Strategy
    except ImportError as e:
        logger.error("无法导入 quant_backtest 或策略模块: %s", e)
        return None, None

    data_cache = DataCache(data_dir=DATA_DIR, duckdb_file='data.duckdb')
    strategy = ICIR_Screen_Strategy(data_dir=DATA_DIR)

    # 预加载因子缓存（当前月 + 前后各 2 个月，覆盖 IC 回看窗口）
    today = date.today()
    start_month = (today.month - 3 - 1) % 12 + 1
    start_year = today.year - 1 if today.month <= 3 else today.year
    end_month = (today.month + 1 - 1) % 12 + 1
    end_year = today.year + 1 if today.month >= 11 else today.year

    start_str = f'{start_year}{start_month:02d}01'
    end_str = f'{end_year}{end_month:02d}28'

    try:
        import pandas as pd
        factor_df = strategy._load_factor_chunk(data_cache, start_str, end_str)
        if factor_df is not None and not factor_df.empty:
            strategy._factor_cache = {
                str(d): grp.drop(columns=['trade_date', 'ts_code'], errors='ignore')
                for d, grp in factor_df.groupby('trade_date')
            }
            logger.info("因子缓存预加载: %d 个交易日, %s ~ %s",
                        len(strategy._factor_cache), start_str, end_str)
    except Exception as e:
        logger.warning("因子缓存预加载失败（将在 _on_select 时按需加载）: %s", e)

    return strategy, data_cache


def _generate_signal(strategy, data_cache, signal_date: str) -> Optional[List[str]]:
    """
    调用 quant_backtest 的 _on_select 生成信号。
    信号存入 strategy._select_cache，同时返回目标列表。
    """
    mock_ctx = _MockContext(current_dt=signal_date, data_cache=data_cache)

    # 确保因子缓存有当天数据
    if signal_date not in getattr(strategy, '_factor_cache', {}):
        _ensure_factor_cache(strategy, data_cache, signal_date)

    try:
        strategy._on_select(mock_ctx)
        # _on_select 将结果放入 strategy._select_cache
        cached = strategy._select_cache.get(signal_date)
        if cached:
            if isinstance(cached, tuple):
                return list(cached[0])
            return list(cached)
    except Exception:
        logger.exception("[信号] _on_select 异常")

    return None


def _ensure_factor_cache(strategy, data_cache, signal_date: str):
    """确保 signal_date 附近的因子数据已缓存。"""
    try:
        start = f'{int(signal_date[:6]) - 1:06d}01'  # 前一个月
        end = f'{int(signal_date[:6]) + 1:02d}28'     # 后一个月
        factor_df = strategy._load_factor_chunk(data_cache, start, end)
        if factor_df is not None and not factor_df.empty:
            cache = getattr(strategy, '_factor_cache', {})
            for d, grp in factor_df.groupby('trade_date'):
                cache[str(d)] = grp.drop(columns=['trade_date', 'ts_code'], errors='ignore')
            strategy._factor_cache = cache
    except Exception as e:
        logger.warning("_ensure_factor_cache 失败: %s", e)


# ═══════════════════════════════════════════════════════════════════════
# Paper Trading 策略入口
# ═══════════════════════════════════════════════════════════════════════

def init(C):
    """策略初始化。"""
    C.target_pool: List[str] = []
    C.select_done_today = False
    C.trade_done_today = False
    C._qt_strategy = None
    C._qt_data_cache = None

    # 初始化 quant_backtest 策略实例
    print("[策略] 初始化 quant_backtest 信号引擎...")
    strategy, dc = _init_quant_strategy()
    if strategy is None:
        print("[策略] ⚠ 信号引擎初始化失败！请检查 quant_backtest 和数据目录。")
        print("[策略] 将以纯文件信号模式运行（需手动放入 signal 文件）。")
        C.target_pool = _load_signals_from_file()
    else:
        C._qt_strategy = strategy
        C._qt_data_cache = dc
        print("[策略] ✓ 信号引擎就绪，将自动生成月度信号。")

    print(f"[策略] IC5_T100_ICIR筛选_月频 已就绪")
    print(f"[策略] 信号文件: {SIGNAL_FILE}")
    print(f"[策略] 当前目标池: {len(C.target_pool) if C.target_pool else 0} 只")


def handlebar(C):
    """分钟级回调。"""
    h, m = C.current_time
    now: datetime = C.timestamper
    today_str = now.strftime('%Y%m%d')
    year, month = now.year, now.month

    # ── 选股日 15:00 ─────────────────────────────────────
    if (h, m) == (15, 0):
        select_day = _get_nth_last_trading_day(year, month, offset=-2)
        if today_str == select_day and not C.select_done_today:
            print(f"\n[策略] {'='*50}")
            print(f"[策略] {today_str} 选股日 — 生成 ICIR 因子信号")
            print(f"[策略] {'='*50}")

            if C._qt_strategy is not None:
                targets = _generate_signal(C._qt_strategy, C._qt_data_cache, today_str)
                if targets:
                    C.target_pool = targets
                    _save_signals_to_file(today_str, targets)
                    print(f"[策略] ✓ 生成 {len(targets)} 只标的")
                else:
                    print("[策略] ✗ 信号生成失败，沿用上次信号")
            else:
                # 文件模式：重新加载
                C.target_pool = _load_signals_from_file()
                print(f"[策略] 从文件加载 {len(C.target_pool) if C.target_pool else 0} 只标的")

            C.select_done_today = True

    # ── 调仓日 09:30 ─────────────────────────────────────
    if (h, m) == (9, 30):
        rebalance_day = _get_nth_last_trading_day(year, month, offset=-1)
        if today_str == rebalance_day and not C.trade_done_today:
            if not C.target_pool:
                print(f"[策略] {today_str} 调仓日 — 无目标池，跳过")
                C.trade_done_today = True
                return

            print(f"\n[策略] {'='*50}")
            print(f"[策略] {today_str} 调仓日 — 目标 {len(C.target_pool)} 只等权")
            print(f"[策略] {'='*50}")
            _rebalance_equal_weight(C)
            C.trade_done_today = True

    # ── 次日凌晨重置标记 ─────────────────────────────────
    if (h, m) == (9, 31):
        C.select_done_today = False
        C.trade_done_today = False


# ═══════════════════════════════════════════════════════════════════════
# 调仓执行
# ═══════════════════════════════════════════════════════════════════════

def _rebalance_equal_weight(C):
    """等权调仓：卖出不在目标池的，买入目标池未持有的。"""
    targets = set(C.target_pool)
    current = C.get_all_positions()
    current_codes = set(current.keys())

    # 卖出
    to_sell = current_codes - targets
    for code in to_sell:
        pos = current[code]
        if pos.available > 0:
            passorder(24, 1102, C.accID, code, pos.available, 0,
                      C.strategy_name, ctx=C)
            print(f"  [卖出] {code} {pos.available}股")

    # 等权买入
    to_buy = targets - current_codes
    if not to_buy:
        print(f"  [策略] 持仓已与目标一致")
        return

    n = max(len(targets), 1)
    per_weight = 1.0 / n
    total_value = C.portfolio_value

    for code in sorted(to_buy):
        tick = C.get_full_tick(code)
        if tick is None or tick.last_price <= 0:
            logger.warning(f"  [跳过] {code} 无行情")
            continue
        amount = total_value * per_weight * 0.98
        qty = int(amount / tick.last_price / 100) * 100
        if qty < 100:
            logger.warning(f"  [跳过] {code} 可买 {qty} 股 < 100")
            continue
        passorder(23, 1102, C.accID, code, qty, 0, C.strategy_name, ctx=C)
        print(f"  [买入] {code} {qty}股 ≈{amount:,.0f}元")


# ═══════════════════════════════════════════════════════════════════════
# 信号文件读写（备用方案：手动运行回测产出信号文件）
# ═══════════════════════════════════════════════════════════════════════

def _load_signals_from_file() -> List[str]:
    try:
        if not os.path.exists(SIGNAL_FILE):
            return []
        with open(SIGNAL_FILE, 'r') as f:
            data = json.load(f)
        return data.get('targets', [])
    except Exception:
        return []


def _save_signals_to_file(signal_date: str, targets: List[str]):
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(SIGNAL_FILE, 'w') as f:
            json.dump({
                'date': signal_date,
                'targets': targets,
                'generated_at': datetime.now().isoformat(),
            }, f, ensure_ascii=False)
        print(f"[策略] 信号已写入: {SIGNAL_FILE}")
    except Exception as e:
        logger.warning("信号文件写入失败: %s", e)
