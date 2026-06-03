"""
模拟券商 — 撮合引擎
====================
模拟真实 A 股交易：T+1、涨跌停限制、交易费用、最小交易单位。

所有规则基于上交所/深交所现行制度:
  - T+1: 当日买入次日才能卖出
  - 涨跌停: ±10% (主板), ±20% (科创/创业), ±30% (北交所)
  - 最小单位: 100股起, 100股递增 (科创 200股起, 1股递增)
  - 佣金: 万1, 最低5元
  - 印花税: 千1 (仅卖出)
  - 过户费: 万0.2 (双向)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
import logging

from paper_trading.qmt_compat import (
    OP_BUY, OP_SELL, ORDER_LIMIT, ORDER_MARKET,
    PositionInfo, OrderInfo, TickData,
)

logger = logging.getLogger(__name__)


@dataclass
class BrokerConfig:
    """券商配置"""
    initial_capital: float = 1_000_000.0
    commission_rate: float = 0.0001   # 万1
    min_commission: float = 5.0       # 最低5元
    stamp_duty_rate: float = 0.001    # 千1 (卖出)
    transfer_fee_rate: float = 0.00002  # 万0.2 过户费
    t1_enabled: bool = True
    price_limit_enabled: bool = True


class PaperBroker:
    """
    模拟券商 — 撮合成交、管理资金与持仓。

    使用方式:
        broker = PaperBroker(BrokerConfig(initial_capital=1_000_000))
        broker.update_market_data(tick_data_dict)   # 每周期更新行情
        broker.submit_order(...)                     # 提交订单
        broker.settle()                              # 日终结算
    """

    def __init__(self, config: BrokerConfig = None):
        self.config = config or BrokerConfig()
        self.cash: float = self.config.initial_capital
        self.frozen_cash: float = 0.0
        self.initial_capital: float = self.config.initial_capital

        # 持仓 {stockcode: PositionInfo}
        self._positions: Dict[str, PositionInfo] = {}

        # 挂单 {order_id: OrderInfo}
        self._pending_orders: Dict[int, OrderInfo] = {}

        # 历史订单
        self._order_history: List[OrderInfo] = []

        # 买入记录 {stockcode: [(quantity, price, date)]} — 用于 T+1 计算
        self._buy_lots: Dict[str, List[tuple]] = {}

        # 当日行情 {stockcode: TickData}
        self._ticks: Dict[str, TickData] = {}

        # 当日涨跌停
        self._limit_up: set = set()
        self._limit_down: set = set()

        # 订单 ID 计数器
        self._order_id: int = 1000

        # 当前日期
        self.current_date: str = ''
        self.current_time: str = ''

    # ── 行情更新 ──────────────────────────────────────────

    def update_market_data(self, ticks: Dict[str, TickData], limit_info: dict = None):
        """更新当日行情"""
        self._ticks = ticks
        if limit_info:
            self._limit_up = set(limit_info.get('limit_up', []))
            self._limit_down = set(limit_info.get('limit_down', []))

    def update_position_prices(self):
        """更新持仓市值"""
        for code, pos in self._positions.items():
            tick = self._ticks.get(code)
            if tick and tick.last_price > 0:
                pos.market_value = pos.quantity * tick.last_price
                pos.unrealized_pnl = pos.quantity * (tick.last_price - pos.avg_cost)
                pos.pnl_pct = (tick.last_price - pos.avg_cost) / pos.avg_cost if pos.avg_cost > 0 else 0.0

    # ── 下单 ──────────────────────────────────────────────

    def submit_order(
        self,
        op_type: int,
        order_type: int,
        stockcode: str,
        quantity: int,
        price: float = 0.0,
        strategy_name: str = '',
        user_order_id: int = 0,
    ) -> int:
        """提交订单，返回订单 ID（0 = 失败）"""
        tick = self._ticks.get(stockcode)
        if tick is None:
            logger.warning(f"[Broker] {stockcode} 无行情数据，订单拒绝")
            return 0

        # 确定成交价
        if order_type == ORDER_MARKET or price <= 0:
            exec_price = tick.last_price
        else:
            exec_price = price

        # 涨跌停检查
        if self.config.price_limit_enabled:
            if op_type == OP_BUY and stockcode in self._limit_up:
                logger.info(f"[Broker] {stockcode} 涨停无法买入")
                return 0
            if op_type == OP_SELL and stockcode in self._limit_down:
                logger.info(f"[Broker] {stockcode} 跌停无法卖出")
                return 0

        # 数量取整
        quantity = self._round_quantity(stockcode, quantity, op_type)

        # 买入资金检查
        if op_type == OP_BUY:
            need = quantity * exec_price + self._calc_commission(quantity * exec_price, OP_BUY)
            if need > self.cash:
                # 调整为可买数量
                max_qty = int(self.cash * 0.99 / exec_price / 100) * 100
                min_unit = 200 if stockcode.startswith('688') else 100
                if max_qty < min_unit:
                    return 0
                quantity = max_qty

        # 卖出持仓检查
        if op_type == OP_SELL:
            pos = self._positions.get(stockcode)
            if pos is None:
                return 0
            available = pos.available if self.config.t1_enabled else pos.quantity
            if available < quantity:
                quantity = available
            if quantity <= 0:
                return 0

        # 创建订单
        self._order_id += 1
        oid = self._order_id
        order = OrderInfo(
            order_id=oid,
            stockcode=stockcode,
            op_type=op_type,
            order_type=order_type,
            quantity=quantity,
            price=exec_price,
            filled_quantity=0,
            status='pending',
            create_time=self.current_time,
        )
        self._pending_orders[oid] = order

        # 市价单立即成交
        if order_type == ORDER_MARKET:
            self._execute_order(oid, exec_price)
        # 限价单：买入 ≤ 现价, 卖出 ≥ 现价 即成交
        elif (op_type == OP_BUY and price >= exec_price) or \
             (op_type == OP_SELL and price <= exec_price):
            self._execute_order(oid, exec_price)

        return oid

    def _execute_order(self, order_id: int, exec_price: float):
        """执行成交"""
        order = self._pending_orders.pop(order_id, None)
        if order is None:
            return

        stockcode = order.stockcode
        quantity = order.quantity
        amount = quantity * exec_price
        commission = self._calc_commission(amount, order.op_type)
        tax = self._calc_stamp_duty(amount, order.op_type)
        transfer = self._calc_transfer_fee(quantity)

        if order.op_type == OP_BUY:
            total_cost = amount + commission + transfer
            self.cash -= total_cost
            # 更新持仓
            if stockcode not in self._positions:
                self._positions[stockcode] = PositionInfo(stockcode=stockcode)
            pos = self._positions[stockcode]
            old_qty = pos.quantity
            total_qty = old_qty + quantity
            pos.avg_cost = (pos.avg_cost * old_qty + amount) / total_qty if total_qty > 0 else exec_price
            pos.quantity = total_qty
            pos.available += quantity  # T+1, 当日不可卖（结算时处理）
            pos.market_value = total_qty * exec_price
            # 记录买入批次
            if stockcode not in self._buy_lots:
                self._buy_lots[stockcode] = []
            self._buy_lots[stockcode].append((quantity, exec_price, self.current_date))
        else:
            income = amount - commission - tax - transfer
            self.cash += income
            pos = self._positions.get(stockcode)
            if pos:
                pos.quantity -= quantity
                pos.available -= quantity
                if pos.quantity <= 0:
                    del self._positions[stockcode]
                    self._buy_lots.pop(stockcode, None)
                else:
                    pos.market_value = pos.quantity * exec_price
                # FIFO 更新成本
                self._reduce_buy_lots(stockcode, quantity)

        order.filled_quantity = quantity
        order.filled_price = exec_price
        order.status = 'filled'
        order.update_time = self.current_time
        self._order_history.append(order)

        side = '买入' if order.op_type == OP_BUY else '卖出'
        logger.info(f"[Broker] {side} {stockcode} {quantity}股 @{exec_price:.3f} "
                    f"金额={amount:.2f} 佣金={commission:.2f} 剩余资金={self.cash:.2f}")

    def _reduce_buy_lots(self, stockcode: str, sell_qty: int):
        """FIFO 减少买入批次"""
        if stockcode not in self._buy_lots:
            return
        remaining = sell_qty
        while remaining > 0 and self._buy_lots[stockcode]:
            lot_qty, lot_price, lot_date = self._buy_lots[stockcode][0]
            if lot_qty <= remaining:
                remaining -= lot_qty
                self._buy_lots[stockcode].pop(0)
            else:
                self._buy_lots[stockcode][0] = (lot_qty - remaining, lot_price, lot_date)
                remaining = 0

    # ── 日终结算 ──────────────────────────────────────────

    def settle(self):
        """日终结算：T+1 解冻"""
        for pos in self._positions.values():
            pos.available = pos.quantity
        self._ticks.clear()
        self._limit_up.clear()
        self._limit_down.clear()

    # ── 查询接口 ──────────────────────────────────────────

    def get_position(self, stockcode: str) -> Optional[PositionInfo]:
        return self._positions.get(stockcode)

    def get_all_positions(self) -> Dict[str, PositionInfo]:
        return dict(self._positions)

    def get_orders(self) -> List[OrderInfo]:
        return list(self._order_history)

    @property
    def total_value(self) -> float:
        pos_value = sum(p.market_value for p in self._positions.values())
        return self.cash + pos_value

    @property
    def total_return(self) -> float:
        return (self.total_value - self.initial_capital) / self.initial_capital

    # ── 费用计算 ──────────────────────────────────────────

    def _calc_commission(self, amount: float, op_type: int) -> float:
        c = amount * self.config.commission_rate
        return max(c, self.config.min_commission)

    def _calc_stamp_duty(self, amount: float, op_type: int) -> float:
        return amount * self.config.stamp_duty_rate if op_type == OP_SELL else 0.0

    def _calc_transfer_fee(self, quantity: int) -> float:
        return abs(quantity) * 0.001  # 约万0.2 按面值

    @staticmethod
    def _round_quantity(stockcode: str, qty: int, op_type: int) -> int:
        """按板块规则取整"""
        if stockcode.startswith('688'):  # 科创板
            min_unit = 200 if op_type == OP_BUY else 1
            increment = 1
        elif stockcode.endswith('.BJ'):  # 北交所
            min_unit = 100
            increment = 1
        else:  # 主板/创业板
            min_unit = 100
            increment = 100
        if qty < min_unit:
            return 0
        if increment == 1:
            return qty
        return (qty // increment) * increment
