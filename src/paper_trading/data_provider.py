"""
数据提供者 — 实时行情接口（占位）
=================================
用户自行对接实时数据源（如 AKShare、Tushare、QMT 数据接口等）。

需实现:
  - get_tick(stockcode) → TickData
  - get_ticks_batch(stockcodes) → Dict[str, TickData]
  - get_limit_info(trade_date) → {'limit_up': [...], 'limit_down': [...]}
  - get_daily_bar(stockcode, trade_date) → OHLCV dict
"""

from __future__ import annotations
from typing import Dict, List, Optional
from paper_trading.qmt_compat import TickData


class DataProvider:
    """
    数据提供者基类 — 占位实现。

    用户替换为自己的数据源后，需实现以下方法。
    """

    def __init__(self):
        self.name = 'base_provider'

    def get_tick(self, stockcode: str) -> Optional[TickData]:
        """
        获取单只股票实时 Tick。

        返回:
            TickData(last_price, open, high, low, volume, amount, bid1, ask1, ...)
        """
        raise NotImplementedError("请实现实时行情接口")

    def get_ticks_batch(self, stockcodes: List[str]) -> Dict[str, TickData]:
        """批量获取 Tick"""
        result = {}
        for code in stockcodes:
            tick = self.get_tick(code)
            if tick:
                result[code] = tick
        return result

    def get_limit_info(self, trade_date: str) -> dict:
        """获取当日涨跌停股票列表"""
        return {'limit_up': [], 'limit_down': []}

    def get_daily_bar(self, stockcode: str, trade_date: str) -> Optional[dict]:
        """获取日线 OHLCV"""
        return None
