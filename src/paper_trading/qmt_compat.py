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
OP_BUY_BASKET = 35    # 篮子买入
OP_SELL_BASKET = 36   # 篮子卖出

# 订单类型
ORDER_LIMIT = 1101    # 限价单
ORDER_MARKET = 1102   # 市价单（实盘模拟中按对手价成交）
ORDER_BASKET_BY_QTY = 2101     # 篮子按份数（volume = 份数）
ORDER_BASKET_BY_AMOUNT = 2102  # 篮子按金额（volume = 元）
ORDER_BASKET_BY_RATIO = 2103   # 篮子按可用资金比例（volume = 0~1）

# 报价类型
PRICE_SELL1 = 4       # 卖一价
PRICE_LATEST = 5      # 最新价
PRICE_BUY1 = 6        # 买一价
PRICE_SPECIFIED = 11  # 指定价


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
    *,
    prType: int = 5,
    modelprice: float = 0.0,
):
    """
    QMT 下单函数 — 签名与 QMT 完全一致。

    单股下单:
        opType:   23=买入, 24=卖出
        orderType: 1101=限价单, 1102=市价单
        stockcode: 证券代码 (如 '000001.SZ')
        quantity:  委托数量 (股)
        price:     委托价格 (限价单必填, 市价单填0)

    篮子下单:
        opType:   35=篮子买入, 36=篮子卖出
        orderType: 2101=按份数, 2102=按金额, 2103=按可用资金比例
        stockcode: 篮子名称 (需先通过 set_basket 注册)
        quantity:  份数(2101) / 金额元(2102) / 比例0~1(2103)
        prType:    4=卖一价, 5=最新价, 6=买一价, 11=指定价

    参数:
        strategyName: 策略名称 (可选)
        quickTrade:  保留参数
        userOrderId: 用户自定义订单 ID
        ctx:         Context 对象
        prType:      篮子报价类型 (仅篮子模式)
        modelprice:  保留参数
    """
    # 篮子下单分发
    if opType in (OP_BUY_BASKET, OP_SELL_BASKET):
        ctx._broker.submit_basket_order(
            op_type=opType,
            order_type=orderType,
            basket_name=stockcode,  # stockcode 解释为篮子名称
            volume=quantity,        # quantity 解释为篮子 volume
            pr_type=prType,
            strategy_name=strategyName or ctx.strategy_name,
            user_order_id=userOrderId,
            baskets=ctx._baskets,
        )
        return

    # 单股下单（原有逻辑）
    ctx._broker.submit_order(
        op_type=opType,
        order_type=orderType,
        stockcode=stockcode,
        quantity=quantity,
        price=price,
        strategy_name=strategyName or ctx.strategy_name,
        user_order_id=userOrderId,
    )


def set_basket(basket_def: dict, ctx: 'Context' = None) -> 'Basket':
    """
    QMT 兼容：注册篮子定义。

    用法:
        set_basket({
            'name': 'my_basket',
            'stocks': [
                {'stock': '600000.SH', 'weight': 0.5, 'optType': 23},
                {'stock': '000001.SZ', 'weight': 0.5, 'optType': 23},
            ]
        }, C)
    """
    if ctx is None:
        raise ValueError("set_basket 需要传入 Context (C) 对象")
    return ctx.set_basket(basket_def)


def order_algo(
    stockcode: str,
    volume: int,
    algo: str = 'TWAP',
    duration: int = 600,
    time_interval: int = 10,
    ctx: 'Context' = None,
) -> int:
    """
    QMT 兼容：简化算法交易接口。

    用法:
        order_algo('000001.SZ', 10000, algo='TWAP', duration=600, time_interval=10, ctx=C)

    参数:
        stockcode:   单股代码 或 篮子名称
        volume:      委托数量（股）
        algo:        'TWAP' 或 'VWAP'
        duration:    执行总时长（秒）
        time_interval: 切片间隔（秒）
        ctx:         Context 对象

    返回 algo_id（0 = 失败）。
    """
    if ctx is None or ctx._broker is None:
        return 0

    # 判断是篮子还是单股
    baskets = getattr(ctx, '_baskets', {}) or {}
    basket = baskets.get(stockcode) if stockcode in baskets else None

    return ctx._broker.submit_algo_order(
        algo_type=algo,
        stockcode='' if basket else stockcode,
        basket_name=stockcode if basket else '',
        volume=volume,
        duration_seconds=duration,
        time_interval_seconds=time_interval,
        strategy_name=ctx.strategy_name,
        baskets=baskets if basket else None,
        current_minute=ctx.barpos,
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


@dataclass
class BasketStock:
    """篮子中的单只股票"""
    stock: str = ''         # 证券代码 '600000.SH'
    weight: float = 0.0     # 权重 0~1（2102/2103 模式使用）
    quantity: int = 0       # 每份股数（2101 模式使用）
    optType: int = 23       # 操作类型: OP_BUY=23 / OP_SELL=24


@dataclass
class Basket:
    """命名篮子 — 通过 set_basket() 注册"""
    name: str = ''
    stocks: list = field(default_factory=list)  # List[BasketStock]
    create_time: str = ''


@dataclass
class BasketOrder:
    """篮子级订单 — 跟踪篮子整体执行状态"""
    basket_id: int = 0
    basket_name: str = ''
    op_type: int = 0            # OP_BUY_BASKET=35 / OP_SELL_BASKET=36
    order_type: int = 0         # 2101 / 2102 / 2103
    volume: float = 0.0         # 份数 / 金额 / 比例
    status: str = 'pending'     # pending / partial / filled / cancelled
    total_child_count: int = 0  # 篮子内股票数
    filled_child_count: int = 0 # 已成交子订单数
    child_order_ids: list = field(default_factory=list)  # List[int]
    total_amount: float = 0.0
    filled_amount: float = 0.0
    create_time: str = ''
    update_time: str = ''
    strategy_name: str = ''


@dataclass
class AlgoOrder:
    """算法交易单 — TWAP/VWAP 拆分执行计划"""
    algo_id: int = 0
    algo_type: str = ''         # 'TWAP' / 'VWAP'
    stockcode: str = ''         # 单股代码（篮子 algo 时为空）
    basket_name: str = ''       # 篮子名称（单股 algo 时为空）
    total_quantity: int = 0     # 总目标股数
    executed_quantity: int = 0  # 已执行股数
    total_amount: float = 0.0
    executed_amount: float = 0.0
    start_minute: int = 0       # 开始分钟索引 0~239
    end_minute: int = 0         # 结束分钟索引
    slice_quantity: int = 0     # 每片股数
    slices_total: int = 0       # 总切片数
    slices_executed: int = 0    # 已完成切片数
    status: str = 'pending'     # pending / active / completed / cancelled
    child_order_ids: list = field(default_factory=list)  # List[int]
    create_time: str = ''
    strategy_name: str = ''
    params: dict = field(default_factory=dict)


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
        self._baskets: Dict[str, Any] = {}  # 篮子注册表 {name: Basket}

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

    # ── 篮子交易 ──────────────────────────────────────

    def set_basket(self, basket_def: dict) -> 'Basket':
        """
        注册篮子定义（QMT 兼容）。

        basket_def = {
            'name': 'my_basket',
            'stocks': [
                {'stock': '600000.SH', 'weight': 0.33, 'optType': 23},
                {'stock': '000001.SZ', 'weight': 0.33, 'optType': 23},
            ]
        }
        """
        name = basket_def['name']
        stocks = [BasketStock(**s) for s in basket_def['stocks']]
        basket = Basket(name=name, stocks=stocks, create_time=self.current_dt)
        self._baskets[name] = basket
        return basket

    def get_basket(self, name: str) -> Optional['Basket']:
        """获取已注册的篮子定义。"""
        return self._baskets.get(name)

    def get_basket_orders(self) -> list:
        """获取所有篮子级订单。"""
        if self._broker is None:
            return []
        return self._broker.get_basket_orders()

    def get_algo_orders(self) -> list:
        """获取所有算法订单。"""
        if self._broker is None:
            return []
        return self._broker.get_algo_orders()

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
