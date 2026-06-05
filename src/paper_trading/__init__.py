"""
paper_trading - 实盘模拟系统
=============================
日度自动化实盘模拟，QMT 兼容策略接口，Web 可视化监控。

主要组件
--------
PaperEngine       : 实盘模拟引擎 (init → handlebar 逐分钟迭代)
PaperBroker       : 模拟券商 (撮合/T+1/涨跌停/费用)
Context           : 策略上下文 (QMT 兼容 API)
DataProvider      : 数据提供者基类 (用户对接实时数据)

QMT 兼容层
----------
passorder()       : 下单函数 (签名与 QMT 一致)
set_basket()      : 篮子定义函数
order_algo()      : 算法交易函数 (TWAP/VWAP)
OP_BUY / OP_SELL  : 买卖方向常量
OP_BUY_BASKET / OP_SELL_BASKET : 篮子买卖常量
ORDER_LIMIT / ORDER_MARKET : 订单类型常量
ORDER_BASKET_BY_QTY/BY_AMOUNT/BY_RATIO : 篮子下单方式

快速开始
--------
>>> from paper_trading import PaperEngine
>>> engine = PaperEngine(strategy_file='strategies/my_strategy.py')
>>> engine.run()

命令行
------
paper-trading run --strategy strategies/my_strategy.py
paper-trading web
"""

from paper_trading._version import __version__
from paper_trading.engine import PaperEngine
from paper_trading.broker import PaperBroker, BrokerConfig
from paper_trading.data_provider import DataProvider
from paper_trading.app import app, update_paper_state
from paper_trading.qmt_compat import (
    Context, passorder, set_basket, order_algo,
    PositionInfo, OrderInfo, TickData,
    Basket, BasketStock, BasketOrder, AlgoOrder,
    OP_BUY, OP_SELL, OP_BUY_BASKET, OP_SELL_BASKET,
    ORDER_LIMIT, ORDER_MARKET,
    ORDER_BASKET_BY_QTY, ORDER_BASKET_BY_AMOUNT, ORDER_BASKET_BY_RATIO,
    PRICE_SELL1, PRICE_LATEST, PRICE_BUY1, PRICE_SPECIFIED,
)

__all__ = [
    'PaperEngine',
    'PaperBroker',
    'BrokerConfig',
    'Context',
    'DataProvider',
    'passorder',
    'set_basket',
    'order_algo',
    'OP_BUY', 'OP_SELL', 'OP_BUY_BASKET', 'OP_SELL_BASKET',
    'ORDER_LIMIT', 'ORDER_MARKET',
    'ORDER_BASKET_BY_QTY', 'ORDER_BASKET_BY_AMOUNT', 'ORDER_BASKET_BY_RATIO',
    'PRICE_SELL1', 'PRICE_LATEST', 'PRICE_BUY1', 'PRICE_SPECIFIED',
    'PositionInfo', 'OrderInfo', 'TickData',
    'Basket', 'BasketStock', 'BasketOrder', 'AlgoOrder',
    'app', 'update_paper_state',
    '__version__',
]
