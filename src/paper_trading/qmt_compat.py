"""
QMT API 兼容层
==============
模拟讯投 QMT 的策略编写接口，策略代码可直接移植到 QMT 实盘。

参考: https://dict.thinktrader.net/freshman/rookie.html

核心约定:
  - 策略文件必须包含 init(C) 和 handlebar(C) 两个函数
  - C 为 Context 对象，提供与 QMT 一致的属性和方法
  - 下单使用 passorder(...) 函数
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from datetime import datetime, time


# ============================================================================
# 下单函数 — 与 QMT passorder 签名完全一致
# ============================================================================

# 操作类型
OP_BUY = 23
OP_SELL = 24

# 订单类型
ORDER_LIMIT = 1101    # 限价单
ORDER_MARKET = 1102   # 市价单（实盘模拟中按对手价成交）


def passorder(
    opType: int,
    orderType: int,
    accountid: str,
    stockcode: str,
    quantity: int,
    price: float,
    strategyName: str = '',
    quickTrade: int = 0,
    userOrderId: int = 0,
    ctx: 'Context' = None,
):
    """
    QMT 下单函数 — 签名与 QMT 完全一致。

    参数:
        opType:      23=买入, 24=卖出
        orderType:   1101=限价单, 1102=市价单
        accountid:   资金账号
        stockcode:   证券代码 (如 '000001.SZ')
        quantity:    委托数量 (股)
        price:       委托价格 (限价单必填, 市价单填0)
        strategyName: 策略名称 (可选)
        quickTrade:  保留参数
        userOrderId: 用户自定义订单 ID
        ctx:         Context 对象 (实盘模拟内部使用)
    """
    ctx._broker.submit_order(
        op_type=opType,
        order_type=orderType,
        stockcode=stockcode,
        quantity=quantity,
        price=price,
        strategy_name=strategyName or ctx.strategy_name,
        user_order_id=userOrderId,
    )


# ============================================================================
# Context — 模拟 QMT 的 C 对象
# ============================================================================

@dataclass
class PositionInfo:
    """持仓信息 — 与 QMT position 对象一致"""
    stockcode: str = ''
    quantity: int = 0          # 总持仓
    available: int = 0          # 可用数量 (T+1 规则)
    avg_cost: float = 0.0      # 持仓成本
    market_value: float = 0.0   # 市值
    unrealized_pnl: float = 0.0 # 浮动盈亏
    pnl_pct: float = 0.0        # 盈亏百分比


@dataclass
class OrderInfo:
    """订单信息"""
    order_id: int = 0
    stockcode: str = ''
    op_type: int = 0
    order_type: int = 0
    quantity: int = 0
    price: float = 0.0
    filled_quantity: int = 0
    filled_price: float = 0.0
    status: str = ''      # 'pending' | 'partial' | 'filled' | 'cancelled' | 'rejected'
    create_time: str = ''
    update_time: str = ''


@dataclass
class TickData:
    """Tick 数据"""
    stockcode: str = ''
    time: str = ''
    last_price: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: int = 0
    amount: float = 0.0
    bid1: float = 0.0
    ask1: float = 0.0
    bid_vol1: int = 0
    ask_vol1: int = 0


class Context:
    """
    策略上下文 — 与 QMT Context (C) 接口一致。

    属性:
        C.accID          资金账号
        C.capital         可用资金
        C.portfolio_value  总资产
        C.barpos          当前 K 线索引 (日频=0, 分钟频=分钟索引)
        C.close           收盘价序列 (C.close[-1] = 最新价)
        C.open / C.high / C.low / C.volume
        C.timestamper     当前时间戳
        C.strategy_name   策略名称

    方法:
        C.get_position(stockcode)       获取持仓
        C.get_full_tick(stockcode)      获取实时行情
        C.get_order_history()           获取委托记录
    """

    def __init__(self, acc_id: str = '', strategy_name: str = 'paper_strategy'):
        self.accID = acc_id
        self.strategy_name = strategy_name

        # 资金
        self.capital: float = 0.0       # 可用资金
        self.portfolio_value: float = 0.0  # 总资产

        # K 线数据 (列表，索引 -1 = 最新)
        self.close: List[float] = []
        self.open: List[float] = []
        self.high: List[float] = []
        self.low: List[float] = []
        self.volume: List[int] = []

        # 当前状态
        self.barpos: int = 0
        self.timestamper: datetime = datetime.now()
        self.current_dt: str = ''  # YYYYMMDD

        # 内部引用
        self._broker: Optional[Any] = None
        self._data_provider: Optional[Any] = None
        self._stock_list: List[str] = []

    # ── QMT 兼容方法 ────────────────────────────────────

    def get_position(self, stockcode: str) -> Optional[PositionInfo]:
        """获取指定股票的持仓（QMT 兼容签名）"""
        if self._broker is None:
            return None
        return self._broker.get_position(stockcode)

    def get_full_tick(self, stockcode: str) -> Optional[TickData]:
        """获取实时 Tick 数据（QMT 兼容签名）"""
        if self._data_provider is None:
            return None
        return self._data_provider.get_tick(stockcode)

    def get_order_history(self) -> List[OrderInfo]:
        """获取历史委托"""
        if self._broker is None:
            return []
        return self._broker.get_orders()

    def get_all_positions(self) -> Dict[str, PositionInfo]:
        """获取全部持仓"""
        if self._broker is None:
            return {}
        return self._broker.get_all_positions()

    # ── QMT 无此方法但实盘模拟需要 ────────────────────────

    def get_close(self, stockcode: str, offset: int = -1) -> float:
        """获取指定股票的历史收盘价"""
        if not self.close:
            return 0.0
        idx = offset % len(self.close)
        return 0.0  # 需 stocks_data 字典支持，简化处理

    def get_history(self, stockcode: str, field: str, count: int):
        """获取历史 K 线数据"""
        return getattr(self, field, [])[-count:] if hasattr(self, field) else []
