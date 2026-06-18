"""
自动调仓模块 — 通用版
=====================
读取策略的 SCHEDULE_CONFIG，自动匹配时间触发选股/调仓。
支持周频、双周频、月频等所有 quant_backtest 调度模式。

用法:
  from paper_trading.auto_trader import AutoTrader
  trader = AutoTrader(store, cfg)
  trader.start()
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, date
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class AutoTrader:
    """
    通用自动调仓器。读取策略 SCHEDULE_CONFIG，按配置时间自动执行。

    SCHEDULE_CONFIG 格式（quant_backtest）:
      [{'func': '_on_select',    'weekday': 4, 'time': '15:00'},   # 周频
       {'func': '_on_rebalance', 'weekday': 5, 'time': '09:30'}]
      [{'func': '_on_select',    'monthday': -2, 'time': '15:00'}, # 月频
       {'func': '_on_rebalance', 'monthday': -1, 'time': '09:30'}]
    """

    def __init__(self, store, cfg: dict):
        self._store = store
        self._cfg = cfg
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._targets: List[str] = []
        self._signal_date = ''
        self._schedule: List[dict] = []       # 解析后的调度表
        self._done_today: Dict[str, bool] = {}  # {func: done}

    # ── 生命周期 ──────────────────────────────────────

    def start(self):
        self._running = True
        self._schedule = self._load_schedule()
        if not self._schedule:
            logger.warning("[AutoTrader] SCHEDULE_CONFIG 为空，自动调仓未启动")
            return
        self._thread = threading.Thread(target=self._run, name="auto-trader", daemon=True)
        self._thread.start()
        logger.info("[AutoTrader] 已启动 (schedule=%d entries)", len(self._schedule))

    def stop(self):
        self._running = False

    # ── 调度加载 ──────────────────────────────────────

    def _load_schedule(self) -> List[dict]:
        """从策略文件读取 SCHEDULE_CONFIG，允许 config.json 覆盖 _on_select 时间。"""
        module_name = self._cfg.get('strategy_module', '')
        collab_root = self._cfg.get('collab_root', '')
        if not module_name:
            return []
        mod = self._load_module(module_name, collab_root)
        for name in dir(mod):
            obj = getattr(mod, name)
            if hasattr(obj, 'SCHEDULE_CONFIG') and isinstance(obj.SCHEDULE_CONFIG, list):
                schedule = [dict(e) for e in obj.SCHEDULE_CONFIG]  # shallow copy
                # config.json 可覆盖信号时间
                select_time = self._cfg.get('select_time', '')
                for entry in schedule:
                    if entry.get('func') == '_on_select' and select_time:
                        entry['time'] = select_time
                return schedule
        return []

    # ── 主循环 ────────────────────────────────────────

    def _run(self):
        import time as _time
        while self._running:
            try:
                self._check_and_act()
            except Exception as e:
                logger.warning("[AutoTrader] 异常: %s", e)
            _time.sleep(60)

    def _check_and_act(self):
        now = datetime.now()
        today = now.date()
        cal_path = self._cfg.get('trade_calendar_path', '')

        for entry in self._schedule:
            func = entry.get('func', '')
            time_str = entry.get('time', '09:30')
            if not self._time_matches(now, time_str):
                continue

            # 检查日期条件
            weekday = entry.get('weekday')
            monthday = entry.get('monthday')
            if weekday is not None:
                # quant_backtest 用 1=周一..5=周五，date.weekday() 是 0=周一
                if (now.weekday() + 1) != weekday:
                    continue
                # 调仓日必须是交易日（假日跳过）
                if func == '_on_rebalance' and not self._is_trading_day(today, cal_path):
                    continue
                # 选股日也检查（假日可正常选股，但如果需要交易日也可加）
                if func == '_on_select' and not self._is_trading_day(today, cal_path):
                    logger.info("[AutoTrader] %s 非交易日，选股跳过", today)
                    continue
            elif monthday is not None:
                if not self._is_monthday(today, monthday):
                    continue
                # monthday 已通过交易日历判断，无需额外检查

            # 当日已执行则跳过
            key = f"{func}_{today.isoformat()}"
            if self._done_today.get(key):
                continue

            # 执行
            if func == '_on_select':
                self._generate_signal(now)
            elif func == '_on_rebalance':
                self._execute_rebalance(now)

            self._done_today[key] = True

    # ── 时间/日期匹配 ─────────────────────────────────

    @staticmethod
    def _time_matches(now: datetime, time_str: str) -> bool:
        """检查当前分钟是否匹配 HH:MM。"""
        parts = time_str.split(':')
        if len(parts) != 2:
            return False
        return now.hour == int(parts[0]) and now.minute == int(parts[1])

    def _is_trading_day(self, today: date, cal_path: str) -> bool:
        """检查今天是否为交易日。"""
        days = self._get_month_trading_days(today.year, today.month, cal_path)
        return today in days

    def _is_monthday(self, today: date, monthday: int) -> bool:
        """检查今天是否为当月第 |monthday| 个交易日（负数=倒数）。"""
        cal_path = self._cfg.get('trade_calendar_path', '')
        trading_days = self._get_month_trading_days(today.year, today.month, cal_path)
        if not trading_days:
            return False
        try:
            target = trading_days[monthday]  # monthday=-1 → 最后, -2 → 倒数第二
            return today == target
        except IndexError:
            return False

    def _get_month_trading_days(self, year: int, month: int, cal_path: str) -> List[date]:
        """获取当月交易日列表。"""
        if cal_path and os.path.exists(cal_path):
            try:
                import pandas as pd
                ext = os.path.splitext(cal_path)[1].lower()
                df = pd.read_parquet(cal_path) if ext in ('.parquet', '.pq') else pd.read_csv(cal_path)
                date_col = df.columns[0]
                all_dates = sorted(pd.to_datetime(df[date_col]).dt.date.unique())
                return [d for d in all_dates if d.year == year and d.month == month]
            except Exception:
                pass
        # 回退：所有工作日
        import calendar
        _, last_day = calendar.monthrange(year, month)
        return [date(year, month, d) for d in range(1, last_day + 1) if date(year, month, d).weekday() < 5]

    # ── 信号生成 ──────────────────────────────────────

    def _generate_signal(self, now: datetime):
        module_name = self._cfg.get('strategy_module', '')
        collab_root = self._cfg.get('collab_root', '')
        data_dir = self._cfg.get('data_dir', '')

        if not module_name:
            logger.warning("[AutoTrader] strategy_module 未配置")
            return

        print(f"\n[AutoTrader] {'='*50}")
        print(f"[AutoTrader] {now.strftime('%Y-%m-%d %H:%M')} 信号生成")
        print(f"[AutoTrader] {'='*50}")

        try:
            if collab_root not in sys.path:
                sys.path.insert(0, collab_root)

            mod = self._load_module(module_name, collab_root)
            StrategyClass = self._find_strategy_class(mod)
            strategy = StrategyClass(data_dir=data_dir)

            from quant_backtest import DataCache
            duckdb_path = self._cfg.get('duckdb_path', os.path.join(data_dir, 'data.duckdb'))
            data_cache = DataCache(data_dir=data_dir, duckdb_file=duckdb_path)

            today_str = now.strftime('%Y%m%d')
            year = int(today_str[:4])
            factor_df = strategy._load_factor_chunk(data_cache, f'{year-1}0101', f'{year}1231')
            if factor_df is None or factor_df.empty:
                print("[AutoTrader] 错误: 因子数据为空")
                return

            factor_cols = [c for c in factor_df.columns if c not in ('trade_date', 'ts_code')]
            strategy._factor_cache = {}
            for d, grp in factor_df.groupby('trade_date'):
                strategy._factor_cache[str(d)] = grp.set_index('ts_code')[factor_cols]

            # 用 _factor_cache 最新日期作为 IC 截止日，信号日用最新因子日期
            signal_date = max(strategy._factor_cache.keys()) if strategy._factor_cache else today_str
            # current_dt 用于 IC 窗口：取 signal_date 对应的交易日（如有日历）或直接用 signal_date
            ic_end_date = signal_date
            print(f"[AutoTrader] 信号日={signal_date}, IC截止日={ic_end_date} (_factor_cache={len(strategy._factor_cache)}天)")

            class BTMock:
                def __init__(self, dc): self._data_cache = dc
            ctx = type('Ctx', (), {'current_dt': ic_end_date, '_backtester': BTMock(data_cache)})()
            strategy._on_select(ctx)

            cached = (strategy._select_cache.get(signal_date)
                      or strategy._select_cache.get(ic_end_date))
            if cached:
                self._targets = list(cached[0]) if isinstance(cached, tuple) else list(cached)
                print(f"[AutoTrader] _select_cache[{signal_date}]: {len(self._targets)} 只")

            if not self._targets:
                self._targets = getattr(strategy, 'candidates', [])
                if self._targets:
                    print(f"[AutoTrader] candidates: {len(self._targets)} 只")

            if not self._targets:
                fc = getattr(strategy, '_factor_cache', {})
                sc = getattr(strategy, '_select_cache', {})
                cv = sc.get(signal_date, 'NOT_FOUND')
                print(f"[AutoTrader] 诊断: _factor_cache={len(fc)}天, "
                      f"cached_value={cv}, type={type(cv).__name__}, "
                      f"signal_date={signal_date} in fc={signal_date in fc}")
                return

            self._signal_date = signal_date
            self._store.save_signal(signal_date, '', self._targets)
            self._store.flush()
            print(f"[AutoTrader] ✓ 信号: {len(self._targets)} 只 (signal_date={signal_date})")

        except Exception as e:
            print(f"[AutoTrader] 信号生成失败: {e}")
            import traceback; traceback.print_exc()

    # ── 调仓执行 ──────────────────────────────────────

    def _execute_rebalance(self, now: datetime):
        targets = self._targets
        if not targets:
            latest = self._store.get_latest_signal()
            if latest:
                targets = latest.get('targets', [])
        if not targets:
            print("[AutoTrader] 无目标池，跳过调仓")
            return

        print(f"\n[AutoTrader] {'='*50}")
        print(f"[AutoTrader] {now.strftime('%Y-%m-%d %H:%M')} 调仓")
        print(f"[AutoTrader] 目标: {len(targets)} 只, 等权")
        print(f"[AutoTrader] {'='*50}")

        try:
            from paper_trading.broker import PaperBroker, BrokerConfig
            from paper_trading.qmt_compat import OP_BUY, OP_SELL, ORDER_MARKET, TickData, PositionInfo

            state = self._store.load_state()
            acc = state.get('account', {})
            cash = acc.get('cash', 0)
            init_cap = acc.get('initial_capital', 100_000_000)

            broker = PaperBroker(BrokerConfig(initial_capital=init_cap))
            broker.current_date = now.strftime('%Y%m%d')
            broker.current_time = '09:30:00'
            broker.cash = cash
            broker.initial_capital = init_cap

            for code, p in state.get('positions', {}).items():
                broker._positions[code] = PositionInfo(
                    stockcode=code, quantity=int(p.get('quantity', 0)),
                    available=int(p.get('quantity', 0)),
                    avg_cost=float(p.get('avg_cost', 0)),
                    market_value=float(p.get('market_value', 0)),
                )

            from paper_trading.data_provider import SinaDataProvider
            provider = SinaDataProvider()
            ticks = provider.get_ticks_batch(targets)
            for code in targets:
                if code not in ticks:
                    ticks[code] = TickData(stockcode=code, last_price=10.0)
            broker.update_market_data(ticks, {})

            current = broker.get_all_positions()
            current_codes = set(current.keys())
            target_set = set(targets)

            # 卖出
            for code in current_codes - target_set:
                qty = current[code].available
                if qty > 0:
                    broker.submit_order(OP_SELL, ORDER_MARKET, code, qty, ticks[code].last_price)

            # 买入
            to_buy = target_set - current_codes
            if to_buy:
                tv = broker.total_value
                w = 1.0 / len(targets)
                for code in sorted(to_buy):
                    tick = ticks.get(code)
                    if not tick or tick.last_price <= 0:
                        continue
                    qty = int(tv * w * 0.98 / tick.last_price / 100) * 100
                    if qty >= 100:
                        broker.submit_order(OP_BUY, ORDER_MARKET, code, qty, tick.last_price)

            broker.settle()
            positions = broker.get_all_positions()

            trade_date = now.strftime('%Y%m%d')
            self._store.save_account({
                'cash': broker.cash, 'total_value': broker.total_value,
                'total_return': broker.total_return, 'initial_capital': broker.initial_capital,
                'position_count': len(positions),
            })
            self._store.save_positions({
                code: {'quantity': p.quantity, 'available': p.available,
                       'avg_cost': p.avg_cost, 'market_value': p.market_value,
                       'unrealized_pnl': p.unrealized_pnl}
                for code, p in positions.items()
            })
            self._store.append_orders([{
                'time': f'{trade_date} 09:30:00', 'stockcode': o.stockcode,
                'side': 'BUY' if o.op_type == 23 else 'SELL',
                'quantity': o.filled_quantity, 'price': o.filled_price,
            } for o in broker.get_orders()])
            self._store.append_nav({
                'date': trade_date, 'nav': broker.total_value / max(init_cap, 1),
                'total_value': broker.total_value, 'cash': broker.cash,
                'position_count': len(positions),
                'daily_return': broker.total_return,
            })
            latest = self._store.get_latest_signal()
            if latest and not latest.get('rebalance_date'):
                self._store.save_signal(latest['signal_date'], trade_date, latest['targets'])
            self._store.flush()

            try:
                from paper_trading.app import update_paper_state
                update_paper_state({
                    'date': trade_date, 'initial_capital': init_cap,
                    'cash': broker.cash, 'total_value': broker.total_value,
                    'total_return': broker.total_return,
                    'positions': {code: {
                        'quantity': p.quantity, 'avg_cost': p.avg_cost,
                        'market_value': p.market_value, 'unrealized_pnl': p.unrealized_pnl,
                    } for code, p in positions.items()},
                    'trades': [{
                        'time': f'{trade_date} 09:30:00', 'stockcode': o.stockcode,
                        'side': 'BUY' if o.op_type == 23 else 'SELL',
                        'quantity': o.filled_quantity, 'price': o.filled_price,
                    } for o in broker.get_orders()],
                    'minute_snapshots': [],
                })
            except Exception:
                pass

            print(f"[AutoTrader] ✓ 调仓完成: 总资产 {broker.total_value:,.0f}")

        except Exception as e:
            print(f"[AutoTrader] 调仓失败: {e}")
            import traceback; traceback.print_exc()

    # ── 策略加载工具 ──────────────────────────────────

    def _load_module(self, module_name: str, collab_root: str):
        import importlib.util as _util
        mod = None
        if module_name.endswith('.py'):
            fpath = module_name if os.path.isabs(module_name) else os.path.join(collab_root, module_name)
            if os.path.exists(fpath):
                spec = _util.spec_from_file_location('_at_strategy', fpath)
                mod = _util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        if mod is None:
            mod = __import__(module_name, fromlist=['*'])
        return mod

    @staticmethod
    def _find_strategy_class(mod) -> type:
        from quant_backtest.strategies import FactorStrategyTemplate
        base_load = FactorStrategyTemplate._load_factor_chunk
        candidates = []
        for name in dir(mod):
            obj = getattr(mod, name)
            if not isinstance(obj, type) or not hasattr(obj, 'NAME'):
                continue
            if obj is FactorStrategyTemplate:
                continue
            try:
                if not issubclass(obj, FactorStrategyTemplate):
                    continue
            except TypeError:
                continue
            if getattr(obj, '_load_factor_chunk', base_load) is base_load:
                continue
            candidates.append(obj)
        if not candidates:
            raise ValueError("未找到覆盖了 _load_factor_chunk 的策略类")
        return max(candidates, key=lambda c: len(c.__mro__))
