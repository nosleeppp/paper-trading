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
    OP_BUY, OP_SELL, OP_BUY_BASKET, OP_SELL_BASKET,
    ORDER_LIMIT, ORDER_MARKET,
    ORDER_BASKET_BY_QTY, ORDER_BASKET_BY_AMOUNT, ORDER_BASKET_BY_RATIO,
    PRICE_SELL1, PRICE_LATEST, PRICE_BUY1, PRICE_SPECIFIED,
    PositionInfo, OrderInfo, TickData,
    Basket, BasketOrder, AlgoOrder,
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

        # 篮子订单跟踪
        self._basket_orders: Dict[int, BasketOrder] = {}
        self._basket_order_id: int = 5000

        # 算法订单跟踪
        self._algo_orders: Dict[int, AlgoOrder] = {}
        self._algo_order_id: int = 6000

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

    # ── 篮子交易 ──────────────────────────────────────────

    def submit_basket_order(
        self,
        op_type: int,
        order_type: int,
        basket_name: str,
        volume: float,
        pr_type: int = PRICE_LATEST,
        strategy_name: str = '',
        user_order_id: int = 0,
        baskets: dict = None,
    ) -> int:
        """
        提交篮子订单。

        返回 basket_order_id（0 = 失败）。
        """
        baskets = baskets or {}
        basket = baskets.get(basket_name)
        if basket is None:
            logger.warning("[Broker] 篮子 '%s' 未定义", basket_name)
            return 0

        # 展开篮子：根据 order_type 计算每只股票的分配
        items = self._expand_basket(basket, op_type, order_type, volume, pr_type)
        if not items:
            logger.warning("[Broker] 篮子 '%s' 展开后无有效标的", basket_name)
            return 0

        # 创建 BasketOrder 记录
        self._basket_order_id += 1
        bid = self._basket_order_id

        bo = BasketOrder(
            basket_id=bid,
            basket_name=basket_name,
            op_type=op_type,
            order_type=order_type,
            volume=volume,
            status='pending',
            total_child_count=len(items),
            create_time=self.current_time,
            strategy_name=strategy_name,
        )

        # 确定子订单方向
        child_side = OP_SELL if op_type == OP_SELL_BASKET else OP_BUY

        # 逐股下单
        for stockcode, qty, exec_price in items:
            child_id = self.submit_order(
                op_type=child_side,
                order_type=ORDER_MARKET,
                stockcode=stockcode,
                quantity=qty,
                price=exec_price,
                strategy_name=strategy_name,
            )
            if child_id > 0:
                bo.child_order_ids.append(child_id)

        bo.filled_child_count = len(bo.child_order_ids)
        bo.status = 'filled' if bo.filled_child_count == bo.total_child_count else 'partial'
        bo.update_time = self.current_time

        self._basket_orders[bid] = bo
        logger.info(
            "[Broker] 篮子 '%s' 下单: %d/%d 成交",
            basket_name, bo.filled_child_count, bo.total_child_count,
        )
        return bid

    def _expand_basket(
        self, basket, op_type: int, order_type: int,
        volume: float, pr_type: int,
    ) -> list:
        """
        展开篮子为 (stockcode, qty, exec_price) 列表。
        """
        items = []

        if order_type == ORDER_BASKET_BY_QTY:
            # volume = 份数，乘以每份股数
            lots = max(int(volume), 1)
            for bs in basket.stocks:
                tick = self._ticks.get(bs.stock)
                price = self._resolve_price(tick, op_type, pr_type)
                if price <= 0:
                    continue
                qty = bs.quantity * lots
                min_unit = 200 if bs.stock.startswith('688') else 100
                if qty >= min_unit:
                    items.append((bs.stock, qty, price))

        elif order_type == ORDER_BASKET_BY_AMOUNT:
            # volume = 总金额（元），按权重分配
            total_amount = float(volume)
            for bs in basket.stocks:
                tick = self._ticks.get(bs.stock)
                price = self._resolve_price(tick, op_type, pr_type)
                if price <= 0 or bs.weight <= 0:
                    continue
                alloc = total_amount * bs.weight
                qty = int(alloc / price / 100) * 100
                min_unit = 200 if bs.stock.startswith('688') else 100
                if qty >= min_unit:
                    items.append((bs.stock, qty, price))

        elif order_type == ORDER_BASKET_BY_RATIO:
            # volume = 0~1，按可用资金比例
            total_amount = self.cash * float(volume)
            for bs in basket.stocks:
                tick = self._ticks.get(bs.stock)
                price = self._resolve_price(tick, op_type, pr_type)
                if price <= 0 or bs.weight <= 0:
                    continue
                alloc = total_amount * bs.weight
                qty = int(alloc / price / 100) * 100
                min_unit = 200 if bs.stock.startswith('688') else 100
                if qty >= min_unit:
                    items.append((bs.stock, qty, price))

        # 买入时做资金约束裁剪
        if op_type == OP_BUY_BASKET and items:
            items = self._trim_basket_items(items, self.cash)

        return items

    @staticmethod
    def _resolve_price(tick, op_type: int, pr_type: int) -> float:
        """根据 pr_type 解析成交参考价。"""
        if tick is None:
            return 0.0
        if pr_type == PRICE_BUY1:
            return tick.bid1 or tick.last_price
        elif pr_type == PRICE_SELL1:
            return tick.ask1 or tick.last_price
        elif pr_type == PRICE_LATEST:
            return tick.last_price
        elif pr_type == PRICE_SPECIFIED:
            return tick.last_price  # 指定价由子订单限价处理
        return tick.last_price

    def _trim_basket_items(self, items: list, available_cash: float) -> list:
        """按比例裁剪超出可用资金的篮子项目。"""
        total_est = sum(qty * price for _, qty, price in items)
        if total_est <= available_cash:
            return items
        scale = (available_cash * 0.98) / total_est
        result = []
        for code, qty, price in items:
            new_qty = int(qty * scale / 100) * 100
            min_unit = 200 if code.startswith('688') else 100
            if new_qty >= min_unit:
                result.append((code, new_qty, price))
        return result

    def get_basket_orders(self) -> list:
        """获取所有篮子订单。"""
        return list(self._basket_orders.values())

    def get_algo_orders(self) -> list:
        """获取所有算法订单。"""
        return list(self._algo_orders.values())

    # ── 算法交易 ──────────────────────────────────────────

    def submit_algo_order(
        self,
        algo_type: str,
        stockcode: str = '',
        basket_name: str = '',
        volume: int = 0,
        duration_seconds: int = 600,
        time_interval_seconds: int = 10,
        strategy_name: str = '',
        baskets: dict = None,
        current_minute: int = 0,
    ) -> int:
        """
        提交算法交易单（TWAP / VWAP）。

        返回 algo_id（0 = 失败）。
        """
        if algo_type not in ('TWAP', 'VWAP'):
            logger.warning("[Broker] 不支持的算法类型: %s", algo_type)
            return 0

        # 计算切片
        duration_minutes = max(duration_seconds // 60, 1)
        interval_minutes = max(time_interval_seconds // 60, 1)
        slice_count = max(duration_minutes // interval_minutes, 1)
        end_min = min(current_minute + duration_minutes, 239)

        total_qty = volume
        slice_qty = max(total_qty // slice_count, 100)

        self._algo_order_id += 1
        aid = self._algo_order_id

        ao = AlgoOrder(
            algo_id=aid,
            algo_type=algo_type,
            stockcode=stockcode,
            basket_name=basket_name,
            total_quantity=total_qty,
            start_minute=current_minute,
            end_minute=end_min,
            slice_quantity=slice_qty,
            slices_total=slice_count,
            status='active',
            create_time=self.current_time,
            strategy_name=strategy_name,
            params={
                'duration_seconds': duration_seconds,
                'time_interval_seconds': time_interval_seconds,
                'volume': volume,
            },
        )

        self._algo_orders[aid] = ao
        logger.info(
            "[Broker] %s 算法创建: id=%d, slices=%d×%d股, 分钟 %d→%d",
            algo_type, aid, slice_count, slice_qty, current_minute, end_min,
        )
        return aid

    def process_algo_orders(self, current_minute: int, baskets: dict = None):
        """
        处理所有活跃算法订单（引擎每分钟调用）。

        执行当前分钟到期的切片。
        """
        baskets = baskets or {}

        for aid, ao in list(self._algo_orders.items()):
            if ao.status != 'active':
                continue

            # 已过结束时间：执行剩余全部
            if current_minute > ao.end_minute:
                remaining = ao.total_quantity - ao.executed_quantity
                if remaining >= 100:
                    self._execute_algo_slice(ao, remaining, baskets)
                ao.status = 'completed'
                continue

            # 执行当前分钟切片
            remaining = ao.total_quantity - ao.executed_quantity
            if remaining <= 0:
                ao.status = 'completed'
                continue

            slice_qty = min(ao.slice_quantity, remaining)
            if slice_qty >= 100:
                self._execute_algo_slice(ao, slice_qty, baskets)

            if ao.executed_quantity >= ao.total_quantity:
                ao.status = 'completed'

    def _execute_algo_slice(self, ao: AlgoOrder, slice_qty: int, baskets: dict):
        """执行算法订单的一个切片。"""
        baskets = baskets or {}

        if ao.basket_name:
            # 篮子 algo：按权重拆分到篮子各股
            basket = baskets.get(ao.basket_name)
            if basket is None:
                return
            total_weight = sum(bs.weight for bs in basket.stocks)
            if total_weight <= 0:
                return
            for bs in basket.stocks:
                if bs.weight <= 0:
                    continue
                tick = self._ticks.get(bs.stock)
                price = tick.last_price if tick and tick.last_price > 0 else 0
                if price <= 0:
                    continue
                stock_qty = max(int(slice_qty * bs.weight / total_weight / 100) * 100, 100)
                child_id = self.submit_order(
                    op_type=OP_BUY if bs.optType == OP_BUY else OP_SELL,
                    order_type=ORDER_MARKET,
                    stockcode=bs.stock,
                    quantity=stock_qty,
                    price=price,
                    strategy_name=ao.strategy_name,
                )
                if child_id > 0:
                    ao.child_order_ids.append(child_id)
        else:
            # 单股 algo
            tick = self._ticks.get(ao.stockcode)
            price = tick.last_price if tick and tick.last_price > 0 else 0
            if price <= 0:
                return
            child_id = self.submit_order(
                op_type=OP_BUY,
                order_type=ORDER_MARKET,
                stockcode=ao.stockcode,
                quantity=slice_qty,
                price=price,
                strategy_name=ao.strategy_name,
            )
            if child_id > 0:
                ao.child_order_ids.append(child_id)

        ao.executed_quantity += slice_qty
        ao.slices_executed += 1
