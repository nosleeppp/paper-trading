#!/usr/bin/env python3
"""
paper_trading 模拟初始化脚本
=============================
以指定日期为模拟起点，执行首次调仓，启动 Web 可视化面板。

用法:
  python scripts/bootstrap_paper.py                          # 默认 5月29日
  python scripts/bootstrap_paper.py --date 20250529          # 指定日期
  python scripts/bootstrap_paper.py --signal signal.json     # 从文件加载信号
  python scripts/bootstrap_paper.py --capital 100000000      # 指定初始资金

流程:
  1. 加载目标股票池（信号文件 / 默认股票池）
  2. 获取实时行情（SinaDataProvider）
  3. 执行等权建仓
  4. 写入 Web 面板共享状态
  5. 启动 Flask Web 服务（http://127.0.0.1:8899）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime

# 确保 paper_trading 包在 path 中
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_PKG_ROOT, 'src'))

from paper_trading.broker import PaperBroker, BrokerConfig
from paper_trading.qmt_compat import OP_BUY, ORDER_MARKET
from paper_trading.data_provider import SinaDataProvider


# ── 默认股票池（信号文件不可用时的回退） ─────────────────
DEFAULT_TARGETS = [
    '600519.SH', '000858.SZ', '601318.SH', '600036.SH', '000333.SZ',
    '600900.SH', '601166.SH', '600276.SH', '000651.SZ', '600887.SH',
    '601888.SH', '600809.SH', '000725.SZ', '601012.SH', '600030.SH',
    '000568.SZ', '600585.SH', '601668.SH', '000002.SZ', '600690.SH',
]  # 20只常见的沪深300/中证500代表股


def load_targets(signal_file: str) -> list:
    """从信号文件加载目标池。"""
    if not signal_file or not os.path.exists(signal_file):
        return []
    try:
        with open(signal_file, 'r') as f:
            data = json.load(f)
        targets = data.get('targets', [])
        print(f"[Bootstrap] 从信号文件加载 {len(targets)} 只标的: {signal_file}")
        return targets
    except Exception as e:
        print(f"[Bootstrap] 信号文件加载失败: {e}")
        return []


def execute_initial_rebalance(targets: list, capital: float, start_date: str):
    """
    执行首次等权建仓。

    1. 创建 Broker
    2. 获取行情
    3. 对目标池等权买入
    4. 返回 report
    """
    config = BrokerConfig(initial_capital=capital)
    broker = PaperBroker(config)
    broker.current_date = start_date
    broker.current_time = '09:30:00'

    # 获取实时行情
    print(f"[Bootstrap] 获取行情数据...")
    provider = SinaDataProvider()
    ticks = provider.get_ticks_batch(targets)
    limit_info = provider.get_limit_info(start_date)
    broker.update_market_data(ticks, limit_info)

    if not ticks:
        print("[Bootstrap] ⚠ 未获取到任何行情数据！使用默认价格 10.0")
        # 回退：为每只股票创建默认 tick
        from paper_trading.qmt_compat import TickData
        for code in targets:
            tick = TickData(stockcode=code, last_price=10.0, bid1=9.99, ask1=10.01)
            ticks[code] = tick
        broker.update_market_data(ticks, limit_info)

    # 等权建仓
    n = len(targets)
    weight = 1.0 / n
    total_value = broker.total_value

    print(f"[Bootstrap] 建仓日: {start_date}")
    print(f"[Bootstrap] 初始资金: {capital:,.0f}")
    print(f"[Bootstrap] 目标标的: {n} 只, 等权={weight*100:.2f}%")

    bought = 0
    for code in targets:
        tick = ticks.get(code)
        if tick is None or tick.last_price <= 0:
            print(f"  [跳过] {code} 无行情")
            continue

        amount = total_value * weight * 0.98  # 留 2% buffer
        qty = int(amount / tick.last_price / 100) * 100
        if qty < 100:
            print(f"  [跳过] {code} 可买{qty}股 < 100")
            continue

        oid = broker.submit_order(
            op_type=OP_BUY,
            order_type=ORDER_MARKET,
            stockcode=code,
            quantity=qty,
            price=tick.last_price,
            strategy_name='bootstrap',
        )
        if oid > 0:
            bought += 1
            print(f"  [买入] {code} {qty}股 @{tick.last_price:.2f}")

    # 结算
    broker.settle()

    # 构建 report
    positions = broker.get_all_positions()
    total_pos_value = sum(p.market_value for p in positions.values())

    report = {
        'date': start_date,
        'initial_capital': broker.initial_capital,
        'cash': broker.cash,
        'total_value': broker.total_value,
        'total_return': broker.total_return,
        'positions': {
            code: {
                'quantity': p.quantity,
                'avg_cost': p.avg_cost,
                'market_value': p.market_value,
                'unrealized_pnl': p.unrealized_pnl,
            }
            for code, p in positions.items()
        },
        'trades': [
            {
                'time': f'{start_date} 09:30:00',
                'stockcode': o.stockcode,
                'side': 'BUY' if o.op_type == OP_BUY else 'SELL',
                'quantity': o.filled_quantity,
                'price': o.filled_price,
            }
            for o in broker.get_orders()
        ],
        'minute_snapshots': [],
    }

    print(f"\n[Bootstrap] 建仓完成:")
    print(f"  成交: {bought}/{n} 只")
    print(f"  总资产: {broker.total_value:,.2f}")
    print(f"  可用资金: {broker.cash:,.2f}")
    print(f"  持仓市值: {total_pos_value:,.2f}")

    return report


def main():
    parser = argparse.ArgumentParser(description='paper_trading 模拟初始化')
    parser.add_argument('--date', '-d', default='20250529',
                        help='建仓日 YYYYMMDD (默认: 20250529)')
    parser.add_argument('--signal', '-s', default=None,
                        help='信号文件路径 (JSON, 含 targets 字段)')
    parser.add_argument('--capital', '-c', type=float, default=100_000_000.0,
                        help='初始资金 (默认: 1亿)')
    parser.add_argument('--port', '-p', type=int, default=8899,
                        help='Web 面板端口 (默认: 8899)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='监听地址 (默认: 0.0.0.0)')
    args = parser.parse_args()

    start_date = args.date
    capital = args.capital
    signal_file = args.signal or os.path.join(
        '/root/lqq_bot_workspace/zz1000/output',
        'paper_signal_targets.json'
    )

    # ── 1. 加载目标池 ─────────────────────────────────
    targets = load_targets(signal_file)
    if not targets:
        print(f"[Bootstrap] 信号文件不可用，使用默认 {len(DEFAULT_TARGETS)} 只股票池")
        print(f"[Bootstrap] 提示: 将信号文件放到 {signal_file}")
        targets = DEFAULT_TARGETS

    # ── 2. 执行首次建仓 ───────────────────────────────
    report = execute_initial_rebalance(targets, capital, start_date)

    # ── 3. 写入 Web 共享状态 ──────────────────────────
    print(f"\n[Bootstrap] 写入 Web 面板状态...")
    try:
        from paper_trading.app import update_paper_state
        update_paper_state(report)
        print("[Bootstrap] ✓ 状态已写入")
    except Exception as e:
        print(f"[Bootstrap] ⚠ 状态写入失败: {e}")

    # ── 4. 启动 Web 面板 ──────────────────────────────
    print(f"\n{'='*60}")
    print(f"  📊 paper_trading Web 面板")
    print(f"  建仓日: {start_date}")
    print(f"  初始资金: {capital:,.0f}")
    print(f"  持仓数: {len(targets)}")
    print(f"  访问: http://127.0.0.1:{args.port}")
    print(f"{'='*60}")

    from paper_trading.app import app
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == '__main__':
    main()
