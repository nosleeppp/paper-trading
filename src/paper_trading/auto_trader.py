"""
自动调仓模块 — 周频策略
=======================
后台线程：周四 21:00 生成信号 → 周五 09:30 执行调仓。

用法:
  from paper_trading.auto_trader import WeeklyAutoTrader
  trader = WeeklyAutoTrader(store, cfg)
  trader.start()
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class WeeklyAutoTrader:
    """
    周频自动调仓器。

    信号日（周四 21:00）：调用 quant_backtest 在线生成下周目标池
    调仓日（周五 09:30）：等权调仓到目标池，持久化到 PaperStore
    """

    def __init__(self, store, cfg: dict):
        self._store = store
        self._cfg = cfg
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._targets: List[str] = []
        self._signal_date = ''
        self._select_done = False
        self._trade_done = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, name="auto-trader", daemon=True)
        self._thread.start()
        logger.info("[AutoTrader] 已启动 (周四21:00选股, 周五09:30调仓)")

    def stop(self):
        self._running = False

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
        weekday = now.weekday()  # 0=Mon ... 3=Thu, 4=Fri
        h, m = now.hour, now.minute

        # ── 周四 21:00:00~21:01:00：生成信号 ──
        if weekday == 3 and h == 21 and m == 0 and not self._select_done:
            self._generate_signal(now)
            self._select_done = True
            self._trade_done = False

        # ── 周五 09:30:00~09:31:00：执行调仓 ──
        if weekday == 4 and h == 9 and m == 30 and not self._trade_done:
            self._execute_rebalance(now)
            self._trade_done = True
            self._select_done = False

        # ── 周六凌晨重置 ──
        if weekday == 5 and h == 0 and m == 0:
            self._select_done = False
            self._trade_done = False

    # ── 信号生成 ──────────────────────────────────────

    def _generate_signal(self, now: datetime):
        module_name = self._cfg.get('strategy_module', '')
        collab_root = self._cfg.get('collab_root', '')
        data_dir = self._cfg.get('data_dir', '')

        if not module_name:
            logger.warning("[AutoTrader] strategy_module 未配置，跳过信号生成")
            return

        print(f"\n[AutoTrader] {'='*50}")
        print(f"[AutoTrader] {now.strftime('%Y-%m-%d %H:%M')} 信号生成开始")
        print(f"[AutoTrader] {'='*50}")

        try:
            if collab_root not in sys.path:
                sys.path.insert(0, collab_root)

            from quant_backtest import DataCache

            # 加载策略模块
            mod = self._load_module(module_name, collab_root)
            StrategyClass = self._find_strategy_class(mod)
            strategy = StrategyClass(data_dir=data_dir)

            duckdb_path = self._cfg.get('duckdb_path', os.path.join(data_dir, 'data.duckdb'))
            data_cache = DataCache(data_dir=data_dir, duckdb_file=os.path.basename(duckdb_path))

            # 加载因子数据
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

            # 运行 _on_select
            class BTMock:
                def __init__(self, dc): self._data_cache = dc
            ctx = type('Ctx', (), {'current_dt': today_str, '_backtester': BTMock(data_cache)})()
            strategy._on_select(ctx)

            cached = strategy._select_cache.get(today_str)
            if cached:
                self._targets = list(cached[0]) if isinstance(cached, tuple) else list(cached)
            else:
                self._targets = getattr(strategy, 'candidates', [])

            if not self._targets:
                print("[AutoTrader] 错误: 未生成信号")
                return

            self._signal_date = today_str
            self._store.save_signal(today_str, '', self._targets)
            self._store.flush()
            print(f"[AutoTrader] 信号已生成: {len(self._targets)} 只标的")
            print(f"[AutoTrader] 信号日: {today_str}")

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
        print(f"[AutoTrader] {now.strftime('%Y-%m-%d %H:%M')} 调仓开始")
        print(f"[AutoTrader] 目标池: {len(targets)} 只, 等权={100/len(targets):.2f}%")
        print(f"[AutoTrader] {'='*50}")

        try:
            from paper_trading.broker import PaperBroker, BrokerConfig
            from paper_trading.qmt_compat import OP_BUY, OP_SELL, ORDER_MARKET

            # 从 store 恢复当前状态
            state = self._store.load_state()
            acc = state.get('account', {})
            cash = acc.get('cash', 0)
            init_cap = acc.get('initial_capital', 100_000_000)

            broker = PaperBroker(BrokerConfig(initial_capital=init_cap))
            broker.current_date = now.strftime('%Y%m%d')
            broker.current_time = '09:30:00'

            # 恢复持仓
            for code, p in state.get('positions', {}).items():
                pi = __import__('paper_trading.qmt_compat', fromlist=['PositionInfo']).PositionInfo(
                    stockcode=code,
                    quantity=int(p.get('quantity', 0)),
                    available=int(p.get('quantity', 0)),
                    avg_cost=float(p.get('avg_cost', 0)),
                    market_value=float(p.get('market_value', 0)),
                )
                broker._positions[code] = pi
            broker.cash = cash
            broker.initial_capital = init_cap

            # 获取实时行情
            from paper_trading.data_provider import SinaDataProvider
            provider = SinaDataProvider()
            ticks = provider.get_ticks_batch(targets)
            # 补充无行情标的
            for code in targets:
                if code not in ticks:
                    from paper_trading.qmt_compat import TickData
                    ticks[code] = TickData(stockcode=code, last_price=10.0)
            broker.update_market_data(ticks, {})

            current_positions = broker.get_all_positions()
            current_codes = set(current_positions.keys())
            target_set = set(targets)

            # 卖出不在目标池的
            to_sell = current_codes - target_set
            for code in to_sell:
                pos = current_positions[code]
                qty = pos.available
                if qty > 0:
                    broker.submit_order(OP_SELL, ORDER_MARKET, code, qty,
                                       ticks.get(code, type('T',(),{'last_price':10})()).last_price)

            # 等权买入新增标的
            to_buy = target_set - current_codes
            if to_buy:
                total_value = broker.total_value
                weight = 1.0 / len(targets)
                for code in sorted(to_buy):
                    tick = ticks.get(code)
                    if not tick or tick.last_price <= 0:
                        continue
                    amount = total_value * weight * 0.98
                    qty = int(amount / tick.last_price / 100) * 100
                    if qty >= 100:
                        broker.submit_order(OP_BUY, ORDER_MARKET, code, qty, tick.last_price)

            broker.settle()
            positions = broker.get_all_positions()

            # 持久化
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
                'time': f'{trade_date} 09:30:00',
                'stockcode': o.stockcode,
                'side': 'BUY' if o.op_type == 23 else 'SELL',
                'quantity': o.filled_quantity, 'price': o.filled_price,
            } for o in broker.get_orders()])
            self._store.append_nav({
                'date': trade_date, 'nav': broker.total_value / max(broker.initial_capital, 1),
                'total_value': broker.total_value, 'cash': broker.cash,
                'position_count': len(positions),
                'daily_return': broker.total_return,
            })
            # 更新信号记录中的 rebalance_date
            latest = self._store.get_latest_signal()
            if latest and not latest.get('rebalance_date'):
                self._store.save_signal(latest['signal_date'], trade_date, latest['targets'])
            self._store.flush()

            # 更新 _paper_state（Web 面板即时可见）
            try:
                from paper_trading.app import update_paper_state
                report = {
                    'date': trade_date, 'initial_capital': broker.initial_capital,
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
                }
                update_paper_state(report)
            except Exception:
                pass

            sold = len(to_sell)
            bought = len(to_buy)
            print(f"[AutoTrader] 调仓完成: 卖出{sold}只, 买入{bought}只")
            print(f"[AutoTrader] 总资产: {broker.total_value:,.0f}")

        except Exception as e:
            print(f"[AutoTrader] 调仓失败: {e}")
            import traceback; traceback.print_exc()

    # ── 工具方法 ──────────────────────────────────────

    def _load_module(self, module_name: str, collab_root: str):
        """加载策略模块（支持文件路径 / dotted path）。"""
        import importlib.util as _util
        mod = None
        if module_name.endswith('.py'):
            if os.path.isabs(module_name) and os.path.exists(module_name):
                spec = _util.spec_from_file_location('_at_strategy', module_name)
                mod = _util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            else:
                fpath = os.path.join(collab_root, module_name)
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
