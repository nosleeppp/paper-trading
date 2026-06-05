"""
实盘模拟引擎
============
每日自动化运行，模拟真实交易全流程。

流程:
  1. before_market (9:00)   — 加载数据、初始化策略
  2. market_open  (9:30-11:30, 13:00-15:00) — 逐分钟迭代
  3. after_market (15:00+)  — 结算、生成报告

使用方式:
    from paper_trading import PaperEngine

    engine = PaperEngine(
        strategy_file='strategies/my_strategy.py',
        initial_capital=1_000_000,
        data_provider=my_provider,
    )
    engine.run()  # 单日运行

调度模式:
    paper-trading run --strategy strategies/my_strategy.py
"""

from __future__ import annotations
import os
import sys
import time
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from paper_trading.broker import PaperBroker, BrokerConfig
from paper_trading.persistence import PaperStore
from paper_trading.qmt_compat import (
    Context, PositionInfo, OrderInfo, TickData,
    passorder, set_basket, order_algo,
    OP_BUY, OP_SELL, OP_BUY_BASKET, OP_SELL_BASKET,
    ORDER_LIMIT, ORDER_MARKET,
    ORDER_BASKET_BY_QTY, ORDER_BASKET_BY_AMOUNT, ORDER_BASKET_BY_RATIO,
)
from paper_trading.data_provider import DataProvider

logger = logging.getLogger(__name__)


class PaperEngine:
    """
    实盘模拟引擎。

    每个交易日运行一次，加载策略 → 模拟交易 → 结算 → 报告。

    参数:
        strategy_file:    策略文件路径 (含 init/bar 函数)
        initial_capital: 初始资金
        data_provider:   实时数据源 (需实现 DataProvider 接口)
        broker_config:   券商费用配置
        stock_list:      股票池 (None=从策略获取)
    """

    def __init__(
        self,
        strategy_file: str,
        initial_capital: float = 1_000_000.0,
        data_provider: 'DataProvider' = None,
        broker_config: BrokerConfig = None,
        stock_list: List[str] = None,
        store: PaperStore = None,
    ):
        self.strategy_file = strategy_file
        self.data_provider = data_provider or DataProvider()
        self.broker_config = broker_config or BrokerConfig(
            initial_capital=initial_capital
        )
        self.stock_list = stock_list or []
        self.store = store  # 持久化存储（可选）

        # 核心组件
        self.broker: Optional[PaperBroker] = None
        self.ctx: Optional[Context] = None
        self.strategy_module: Optional[Any] = None

        # 日运行状态
        self.current_date: str = ''
        self.current_time: str = ''
        self.is_running: bool = False
        self._minute_index: int = 0

        # 日志 / 报告
        self._trade_log: List[dict] = []
        self._minute_snapshots: List[dict] = []

    # ── 单日运行 ──────────────────────────────────────────

    def run(self, trade_date: str = None) -> dict:
        """
        运行单个交易日的模拟。

        返回: {'nav': float, 'trades': [...], 'positions': {...}, ...}
        """
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y%m%d')

        self.current_date = trade_date
        self.is_running = True
        self._trade_log = []
        self._minute_snapshots = []
        self._minute_index = 0

        # 0. 从 store 恢复状态（幂等启动）
        if self.store:
            self.store.init_db()
            state = self.store.load_state()
            if state.get('has_positions'):
                logger.info("[Engine] 从 DB 恢复状态 (positions=%d)",
                            len(state.get('positions', {})))
                # broker 在 _before_market 中创建，这里先暂存恢复数据
                self._restored_state = state

        try:
            # 1. 盘前准备
            self._before_market()

            # 2. 盘中交易（逐分钟）
            self._market_session()

            # 3. 盘后结算
            self._after_market()

            report = self._build_report()

            # 4. 持久化
            if self.store:
                self._save_to_store(report)

            # 5. 写入 Web 面板共享状态
            try:
                from paper_trading.app import update_paper_state
                update_paper_state(report)
            except Exception:
                pass

            return report
        finally:
            self.is_running = False

    # ── 盘前 ──────────────────────────────────────────────

    def _before_market(self):
        """9:00 盘前准备"""
        logger.info(f"[Engine] {self.current_date} 盘前准备开始")
        print(f"\n{'='*60}")
        print(f"  实盘模拟 — {self.current_date}")
        print(f"{'='*60}")

        # 初始化券商
        self.broker = PaperBroker(self.broker_config)
        self.broker.current_date = self.current_date

        # 从 store 恢复状态（幂等）
        if self.store and hasattr(self, '_restored_state'):
            self.broker.restore(self._restored_state)
            del self._restored_state

        # 创建 Context
        self.ctx = Context(
            acc_id='PAPER_001',
            strategy_name='paper_strategy',
        )
        self.ctx._broker = self.broker
        self.ctx._data_provider = self.data_provider
        self.ctx.current_dt = self.current_date
        self.ctx.capital = self.broker.cash
        self.ctx.portfolio_value = self.broker.total_value

        # 加载策略
        self._load_strategy()

        # 调用策略 init(C)
        if hasattr(self.strategy_module, 'init'):
            try:
                self.strategy_module.init(self.ctx)
                logger.info("[Engine] 策略 init() 完成")
            except Exception as e:
                logger.error(f"[Engine] 策略 init() 异常: {e}")
                traceback.print_exc()

        # 预设股票池
        if not self.stock_list and hasattr(self.ctx, '_stock_list'):
            self.stock_list = self.ctx._stock_list

    # ── 盘中 ──────────────────────────────────────────────

    def _market_session(self):
        """逐分钟运行交易时段"""
        minutes = self._trading_minutes()
        print(f"[Engine] 盘中交易: {len(minutes)} 分钟")

        for h, m in minutes:
            self.current_time = f'{h:02d}:{m:02d}:00'
            self.ctx.current_time = (h, m)
            self.ctx.timestamper = datetime.strptime(
                f'{self.current_date} {self.current_time}', '%Y%m%d %H:%M:%S'
            )
            self._minute_index += 1

            # 更新行情
            self._update_ticks()

            # 更新持仓价格
            self.broker.update_position_prices()
            self.ctx.capital = self.broker.cash
            self.ctx.portfolio_value = self.broker.total_value

            # 调用策略 handlebar(C)
            if hasattr(self.strategy_module, 'handlebar'):
                try:
                    self.strategy_module.handlebar(self.ctx)
                except Exception as e:
                    logger.error(f"[Engine] handlebar 异常 {self.current_time}: {e}")

            # 执行算法订单切片（TWAP/VWAP）
            self.broker.process_algo_orders(
                current_minute=self._minute_index,
                baskets=self.ctx._baskets,
            )
            # 算法执行后刷新资金状态
            self.ctx.capital = self.broker.cash
            self.ctx.portfolio_value = self.broker.total_value

            # 记录分钟快照
            self._minute_snapshots.append({
                'time': self.current_time,
                'capital': self.broker.cash,
                'total_value': self.broker.total_value,
                'positions': len(self.broker.get_all_positions()),
            })

        print(f"[Engine] 盘中交易结束")

    def _update_ticks(self):
        """更新当日行情（从 data_provider 获取）"""
        if not self.stock_list:
            return
        try:
            ticks = self.data_provider.get_ticks_batch(self.stock_list)
            limit_info = self.data_provider.get_limit_info(self.current_date)
            self.broker.update_market_data(ticks, limit_info)
        except NotImplementedError:
            pass  # 占位模式
        except Exception as e:
            logger.warning(f"[Engine] 行情更新异常: {e}")

    # ── 盘后 ──────────────────────────────────────────────

    def _after_market(self):
        """15:00 盘后结算"""
        self.broker.settle()
        self.ctx.capital = self.broker.cash
        self.ctx.portfolio_value = self.broker.total_value
        logger.info(f"[Engine] {self.current_date} 结算完成")

    # ── 报告 ──────────────────────────────────────────────

    def _build_report(self) -> dict:
        """生成日度报告"""
        orders = self.broker.get_orders()
        positions = self.broker.get_all_positions()
        today_orders = [o for o in orders if o.create_time.startswith(self.current_date)]

        return {
            'date': self.current_date,
            'initial_capital': self.broker.initial_capital,
            'cash': self.broker.cash,
            'total_value': self.broker.total_value,
            'total_return': self.broker.total_return,
            'positions': {code: {
                'quantity': p.quantity,
                'avg_cost': p.avg_cost,
                'market_value': p.market_value,
                'unrealized_pnl': p.unrealized_pnl,
            } for code, p in positions.items()},
            'trades': [{
                'time': o.update_time,
                'stockcode': o.stockcode,
                'side': 'BUY' if o.op_type == OP_BUY else 'SELL',
                'quantity': o.filled_quantity,
                'price': o.filled_price,
            } for o in today_orders],
            'basket_orders': [{
                'basket_id': bo.basket_id,
                'basket_name': bo.basket_name,
                'status': bo.status,
                'volume': bo.volume,
                'child_count': bo.total_child_count,
                'filled_count': bo.filled_child_count,
            } for bo in self.broker.get_basket_orders()],
            'algo_orders': [{
                'algo_id': ao.algo_id,
                'algo_type': ao.algo_type,
                'stockcode': ao.stockcode,
                'basket_name': ao.basket_name,
                'status': ao.status,
                'total_quantity': ao.total_quantity,
                'executed_quantity': ao.executed_quantity,
                'slices_total': ao.slices_total,
                'slices_executed': ao.slices_executed,
            } for ao in self.broker.get_algo_orders()],
            'minute_snapshots': self._minute_snapshots,
        }

    def print_summary(self, report: dict = None):
        """打印日度摘要"""
        if report is None:
            report = self._build_report()
        print(f"\n{'='*50}")
        print(f"  实盘模拟日度报告 — {report['date']}")
        print(f"{'='*50}")
        print(f"  总资产:       {report['total_value']:,.2f}")
        print(f"  可用资金:     {report['cash']:,.2f}")
        print(f"  累计收益:     {report['total_return']:+.2%}")
        print(f"  持仓数:       {len(report['positions'])}")
        print(f"  今日成交:     {len(report['trades'])} 笔")
        if report.get('basket_orders'):
            filled = sum(1 for bo in report['basket_orders'] if bo['status'] == 'filled')
            print(f"  篮子订单:     {len(report['basket_orders'])} 个 ({filled} 已完成)")
        if report.get('algo_orders'):
            completed = sum(1 for ao in report['algo_orders'] if ao['status'] == 'completed')
            print(f"  算法订单:     {len(report['algo_orders'])} 个 ({completed} 已完成)")
        print(f"{'='*50}")

    # ── 策略加载 ──────────────────────────────────────────

    def _load_strategy(self):
        """加载策略文件"""
        if not os.path.exists(self.strategy_file):
            raise FileNotFoundError(f"策略文件不存在: {self.strategy_file}")

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            '_paper_strategy', self.strategy_file
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules['_paper_strategy'] = mod
        spec.loader.exec_module(mod)
        self.strategy_module = mod

        # 将 QMT 兼容 API 注入到策略模块命名空间
        mod.passorder = passorder
        mod.set_basket = set_basket
        mod.order_algo = order_algo
        mod.OP_BUY = OP_BUY
        mod.OP_SELL = OP_SELL
        mod.OP_BUY_BASKET = OP_BUY_BASKET
        mod.OP_SELL_BASKET = OP_SELL_BASKET
        mod.ORDER_LIMIT = ORDER_LIMIT
        mod.ORDER_MARKET = ORDER_MARKET
        mod.ORDER_BASKET_BY_QTY = ORDER_BASKET_BY_QTY
        mod.ORDER_BASKET_BY_AMOUNT = ORDER_BASKET_BY_AMOUNT
        mod.ORDER_BASKET_BY_RATIO = ORDER_BASKET_BY_RATIO

    # ── 持久化 ──────────────────────────────────────────

    def _save_to_store(self, report: dict) -> None:
        """将 report 写入 PaperStore。"""
        if not self.store:
            return
        # 账户
        self.store.save_account({
            'cash': report.get('cash', 0),
            'total_value': report.get('total_value', 0),
            'total_return': report.get('total_return', 0),
            'initial_capital': report.get('initial_capital', 0),
            'position_count': len(report.get('positions', {})),
        })
        # 持仓
        self.store.save_positions(report.get('positions', {}))
        # 成交
        self.store.append_orders(report.get('trades', []))
        # 净值
        snapshots = report.get('minute_snapshots', [])
        if snapshots:
            last = snapshots[-1]
            init_cap = report.get('initial_capital', 1)
            nav = last.get('total_value', 0) / max(init_cap, 1)
            self.store.append_nav({
                'date': report.get('date', ''),
                'nav': nav,
                'total_value': last.get('total_value', 0),
                'cash': last.get('capital', 0),
                'position_count': last.get('positions', 0),
                'daily_return': report.get('total_return', 0),
            })
        self.store.flush()
        logger.info("[Engine] 状态已持久化到 %s", self.store._db_path)

    # ── 工具 ──────────────────────────────────────────────

    @staticmethod
    def _trading_minutes() -> List[tuple]:
        """A 股交易分钟列表"""
        minutes = []
        for m in range(120):  # 9:30-11:30
            h, rem = divmod(9 * 60 + 30 + m, 60)
            minutes.append((h, rem))
        for m in range(120):  # 13:00-15:00
            h, rem = divmod(13 * 60 + m, 60)
            minutes.append((h, rem))
        return minutes
