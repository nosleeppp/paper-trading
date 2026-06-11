"""
数据提供者 — 实时行情接口
=========================
支持两种实时数据获取路径：

  1. WebSocketDataProvider
     ws://219.143.246.7:18888/market
     订阅: {"subList": ["688571.SH", "688575.SH"], "type": "SUB"}

  2. SinaDataProvider
     新浪财经 HTTP 接口 (http://hq.sinajs.cn/list=...)

用法:
  from paper_trading.data_provider import WebSocketDataProvider, SinaDataProvider

  ws_provider = WebSocketDataProvider(["688571.SH", "688575.SH"])
  ws_provider.connect()
  tick = ws_provider.get_tick("688571.SH")
  ws_provider.disconnect()

  sina_provider = SinaDataProvider()
  tick = sina_provider.get_tick("sh688571")
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Dict, List, Optional

import requests

from paper_trading.qmt_compat import TickData

logger = logging.getLogger(__name__)

# ── WebSocket 行情服务器配置 ──────────────────────────────────────────
WS_MARKET_URL = "ws://219.143.246.7:18888/market"

# ── 新浪财经配置 ──────────────────────────────────────────────────────
SINA_API_URL = "http://hq.sinajs.cn/list={codes}"


def _parse_sina_response(text: str, stockcode: str) -> Optional[TickData]:
    """解析新浪财经单条行情响应。"""
    try:
        # 格式: var hq_str_sh600519="贵州茅台,1761.00,...";
        if '"' not in text or len(text.split('"')) < 2:
            logger.warning("新浪行情响应格式异常: %s", text[:80])
            return None
        data_str = text.split('"')[1]
        fields = data_str.split(",")
        if len(fields) < 32:
            logger.warning("新浪行情字段不足 (%d): %s", len(fields), data_str[:80])
            return None

        tick = TickData()
        tick.stockcode = stockcode
        tick.last_price = float(fields[3]) if fields[3] else 0.0
        tick.open = float(fields[1]) if fields[1] else 0.0
        tick.high = float(fields[4]) if fields[4] else 0.0
        tick.low = float(fields[5]) if fields[5] else 0.0
        tick.volume = int(float(fields[8])) if fields[8] else 0
        tick.amount = float(fields[9]) if fields[9] else 0.0
        tick.bid1 = float(fields[6]) if fields[6] else 0.0
        tick.ask1 = float(fields[7]) if fields[7] else 0.0
        # 新浪接口含五档买卖盘，日期/时间在 fields[30]/[31]
        tick.time = f"{fields[30]} {fields[31]}" if fields[30] else ""
        return tick
    except (ValueError, IndexError) as e:
        logger.warning("解析新浪行情失败: %s", e)
        return None


def _parse_ws_message(raw: str) -> Dict[str, TickData]:
    """解析 WebSocket 推送的行情数据。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("非 JSON 消息: %s", raw[:80])
        return {}

    # 心跳/非行情消息（int/str/list 等）
    if not isinstance(data, dict):
        return {}

    # 消息格式: {"688571.SH": {"newestPrice": 7.51, "open": 7.24, ...}, ...}
    ticks: Dict[str, TickData] = {}
    for code, item in data.items():
        if not isinstance(item, dict):
            continue
        tick = TickData()
        tick.stockcode = code
        tick.last_price = float(item.get("newestPrice", 0))
        tick.open = float(item.get("open", 0))
        tick.high = float(item.get("high", 0))
        tick.low = float(item.get("low", 0))
        tick.volume = int(item.get("volume", 0))
        tick.amount = float(item.get("amount", 0))
        # 买卖五档 — 取第一档
        biddings = item.get("biddings") or []
        askings = item.get("askings") or []
        bidding_amounts = item.get("biddingAmounts") or []
        asking_amounts = item.get("askingAmounts") or []
        tick.bid1 = float(biddings[0]) if biddings else 0.0
        tick.ask1 = float(askings[0]) if askings else 0.0
        tick.bid_vol1 = int(bidding_amounts[0]) if bidding_amounts else 0
        tick.ask_vol1 = int(asking_amounts[0]) if asking_amounts else 0
        # 时间戳
        ts = item.get("time")
        if ts:
            tick.time = str(ts)
        ticks[code] = tick
    return ticks


class WebSocketDataProvider:
    """
    WebSocket 实时行情数据提供者。

    连接到 ws://219.143.246.7:18888/market，
    订阅指定的股票代码，在后台线程中持续接收推送数据。

    用法:
        provider = WebSocketDataProvider(["688571.SH", "688575.SH"])
        provider.connect()
        tick = provider.get_tick("688571.SH")
        ticks = provider.get_ticks_batch(["688571.SH", "688575.SH"])
        provider.disconnect()
    """

    def __init__(self, symbols: List[str], ws_url: str = WS_MARKET_URL):
        self.name = "websocket"
        self._ws_url = ws_url
        self._symbols = list(symbols)
        self._lock = threading.Lock()
        self._latest_ticks: Dict[str, TickData] = {}
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_app = None
        self._running = False

    # ── 公开 API ──────────────────────────────────────────────────

    def connect(self) -> None:
        """建立 WebSocket 连接并在后台线程接收数据。"""
        if self._running:
            logger.warning("WebSocket 已连接，跳过重复 connect")
            return
        self._running = True
        self._ws_thread = threading.Thread(
            target=self._run_forever,
            name="ws-market",
            daemon=True,
        )
        self._ws_thread.start()
        logger.info("WebSocket 连接线程已启动: %s", self._ws_url)

    def disconnect(self) -> None:
        """关闭 WebSocket 连接。"""
        self._running = False
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception:
                pass
        logger.info("WebSocket 已断开")

    def get_tick(self, stockcode: str) -> Optional[TickData]:
        """获取单只股票的最新 Tick（从缓存读取）。"""
        with self._lock:
            return self._latest_ticks.get(stockcode)

    def get_ticks_batch(self, stockcodes: List[str]) -> Dict[str, TickData]:
        """批量获取最新 Tick。"""
        result: Dict[str, TickData] = {}
        with self._lock:
            for code in stockcodes:
                tick = self._latest_ticks.get(code)
                if tick:
                    result[code] = tick
        return result

    def get_limit_info(self, trade_date: str) -> dict:
        """WebSocket 不提供涨跌停列表。"""
        return {"limit_up": [], "limit_down": []}

    def get_daily_bar(self, stockcode: str, trade_date: str) -> Optional[dict]:
        """WebSocket 不提供历史日线。"""
        return None

    # ── 内部 ─────────────────────────────────────────────────────

    def _run_forever(self) -> None:
        """后台线程：持续维护 WebSocket 连接。"""
        # 延迟导入，避免强依赖
        import websocket

        while self._running:
            try:
                ws = websocket.WebSocketApp(
                    self._ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_app = ws
                # 阻塞运行，直到连接关闭
                ws.run_forever()
            except Exception:
                logger.warning("WebSocket 异常，5 秒后重连", exc_info=True)
            if self._running:
                self._ws_app = None
                threading.Event().wait(5)  # 重连间隔

    def _on_open(self, ws_app) -> None:
        """连接成功后发送订阅请求。"""
        sub_msg = json.dumps({
            "subList": self._symbols,
            "type": "SUB",
        })
        ws_app.send(sub_msg)
        logger.info("WebSocket 订阅已发送: %s", sub_msg)

    def _on_message(self, _ws_app, message: str) -> None:
        """收到推送数据后更新缓存。"""
        ticks = _parse_ws_message(message)
        if ticks:
            with self._lock:
                self._latest_ticks.update(ticks)

    def _on_error(self, _ws_app, error) -> None:
        logger.error("WebSocket 错误: %s", error)

    def _on_close(self, _ws_app, close_status_code, _close_msg) -> None:
        logger.info("WebSocket 连接关闭 (code=%s)", close_status_code)


class SinaDataProvider:
    """
    新浪财经实时行情数据提供者。

    通过 HTTP 接口获取实时行情，股票代码使用新浪格式：
    - 上交所: sh + 代码 (如 sh688571)
    - 深交所: sz + 代码 (如 sz000001)

    用法:
        provider = SinaDataProvider()
        tick = provider.get_tick("sh688571")
        ticks = provider.get_ticks_batch(["sh688571", "sz000001"])
    """

    def __init__(self):
        self.name = "sina"
        self._session = requests.Session()
        self._session.headers.update({
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko)"
            ),
        })

    @staticmethod
    def to_sina_code(stockcode: str) -> str:
        """
        将标准代码转为新浪格式。

        >>> SinaDataProvider.to_sina_code("688571.SH")
        'sh688571'
        >>> SinaDataProvider.to_sina_code("000001.SZ")
        'sz000001'
        """
        if "." in stockcode:
            code, market = stockcode.split(".")
            return f"{market.lower()}{code}"
        return stockcode

    # ── 公开 API ──────────────────────────────────────────────────

    def get_tick(self, stockcode: str) -> Optional[TickData]:
        """获取单只股票实时行情。"""
        sina_code = self.to_sina_code(stockcode)
        url = SINA_API_URL.format(codes=sina_code)
        try:
            resp = self._session.get(url, timeout=5)
            resp.encoding = "gbk"
            if resp.status_code != 200:
                logger.warning("新浪请求失败: HTTP %s", resp.status_code)
                return None
            return _parse_sina_response(resp.text, stockcode)
        except requests.RequestException as e:
            logger.warning("新浪请求异常: %s", e)
            return None

    def get_ticks_batch(self, stockcodes: List[str]) -> Dict[str, TickData]:
        """批量获取实时行情（合并为单次请求）。"""
        sina_codes = ",".join(self.to_sina_code(c) for c in stockcodes)
        url = SINA_API_URL.format(codes=sina_codes)
        result: Dict[str, TickData] = {}
        try:
            resp = self._session.get(url, timeout=5)
            resp.encoding = "gbk"
            if resp.status_code != 200:
                logger.warning("新浪批量请求失败: HTTP %s", resp.status_code)
                return result
            # 响应包含多行 var hq_str_xxx="...";
            lines = resp.text.strip().split("\n")
            # 按发送顺序对应
            for i, line in enumerate(lines):
                if i >= len(stockcodes):
                    break
                tick = _parse_sina_response(line, stockcodes[i])
                if tick:
                    result[stockcodes[i]] = tick
        except requests.RequestException as e:
            logger.warning("新浪批量请求异常: %s", e)
        return result

    def get_limit_info(self, trade_date: str) -> dict:
        """新浪接口不直接提供涨跌停列表。"""
        return {"limit_up": [], "limit_down": []}

    def get_daily_bar(self, stockcode: str, trade_date: str) -> Optional[dict]:
        """新浪接口不提供历史日线。"""
        return None


# ── 默认 DataProvider 别名（向后兼容） ─────────────────────────────────
DataProvider = SinaDataProvider
