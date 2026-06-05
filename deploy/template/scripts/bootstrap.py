#!/usr/bin/env python3
"""
paper_trading 通用初始化脚本
============================
读取 config.json，执行首次建仓，启动 Web 可视化面板。

用法:
  python scripts/bootstrap.py                     # 使用 config.json 默认配置
  python scripts/bootstrap.py --signal signals/custom.json
  python scripts/bootstrap.py --date 20250630 --capital 50000000
  python scripts/bootstrap.py --auto-signal        # 在线信号生成
"""

from __future__ import annotations

import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(ROOT_DIR, 'config.json')


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        print("[Bootstrap] 未找到 config.json，使用默认配置")
        return {}
    with open(CONFIG_PATH, 'r') as f:
        cfg = json.load(f)
    return {k: v for k, v in cfg.items() if not k.startswith('_')}


def load_targets(signal_file: str) -> list:
    if not signal_file or not os.path.exists(signal_file):
        return []
    try:
        with open(signal_file, 'r') as f:
            data = json.load(f)
        return data.get('targets', [])
    except Exception as e:
        print(f"[Bootstrap] 信号文件加载失败: {e}")
        return []


def execute_rebalance(targets: list, capital: float, start_date: str) -> dict:
    """执行首次等权建仓。"""
    from paper_trading.broker import PaperBroker, BrokerConfig
    from paper_trading.qmt_compat import OP_BUY, ORDER_MARKET, TickData
    from paper_trading.data_provider import SinaDataProvider

    config = BrokerConfig(initial_capital=capital)
    broker = PaperBroker(config)
    broker.current_date = start_date
    broker.current_time = '09:30:00'

    print("[Bootstrap] 获取实时行情...")
    provider = SinaDataProvider()
    ticks = provider.get_ticks_batch(targets)
    limit_info = provider.get_limit_info(start_date)
    broker.update_market_data(ticks, limit_info)

    for code in targets:
        if code not in ticks:
            ticks[code] = TickData(stockcode=code, last_price=10.0, bid1=9.99, ask1=10.01)
    broker.update_market_data(ticks, limit_info)

    n = len(targets)
    weight = 1.0 / n
    total_value = broker.total_value
    bought = 0

    for code in targets:
        tick = ticks.get(code)
        if tick is None or tick.last_price <= 0:
            continue
        amount = total_value * weight * 0.98
        qty = int(amount / tick.last_price / 100) * 100
        if qty < 100:
            continue
        oid = broker.submit_order(
            op_type=OP_BUY, order_type=ORDER_MARKET,
            stockcode=code, quantity=qty, price=tick.last_price,
            strategy_name='bootstrap',
        )
        if oid > 0:
            bought += 1

    broker.settle()
    positions = broker.get_all_positions()

    return {
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


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(description='paper_trading 初始化')
    parser.add_argument('--date', '-d', default=cfg.get('start_date', '20250601'))
    parser.add_argument('--signal', '-s', default=cfg.get('signal_file'))
    parser.add_argument('--capital', '-c', type=float, default=cfg.get('capital', 100_000_000))
    parser.add_argument('--port', '-p', type=int, default=cfg.get('web', {}).get('port', 8899))
    parser.add_argument('--host', default=cfg.get('web', {}).get('host', '0.0.0.0'))
    parser.add_argument('--auto-signal', action='store_true')
    args = parser.parse_args()

    start_date = args.date
    capital = args.capital
    signal_file = args.signal
    if signal_file and not os.path.isabs(signal_file):
        signal_file = os.path.join(ROOT_DIR, signal_file)

    print(f"\n{'='*60}")
    print(f"  paper_trading 模拟初始化")
    print(f"  建仓日: {start_date}    初始资金: {capital:,.0f}")
    print(f"{'='*60}\n")

    # ── 1. 目标池 ──────────────────────────────────
    targets = load_targets(signal_file) if signal_file else []

    if not targets:
        targets = cfg.get('default_targets', [])
        if targets:
            print(f"[Bootstrap] 使用 config.json 默认股票池: {len(targets)} 只")
        else:
            print("[Bootstrap] 错误: 无可用标的。请提供 --signal 或配置 default_targets")
            sys.exit(1)

    # 保存信号
    os.makedirs(os.path.join(ROOT_DIR, 'signals'), exist_ok=True)
    out_signal = os.path.join(ROOT_DIR, 'signals', 'paper_signal_targets.json')
    with open(out_signal, 'w') as f:
        json.dump({'date': start_date, 'targets': targets,
                    'generated_at': __import__('datetime').datetime.now().isoformat()},
                   f, ensure_ascii=False)
    print(f"[Bootstrap] 信号已保存: {out_signal}")

    # ── 2. 建仓 ────────────────────────────────────
    report = execute_rebalance(targets, capital, start_date)
    print(f"  成交: {len(report['trades'])}/{len(targets)} 只")
    print(f"  总资产: {report['total_value']:,.2f}")

    # ── 3. Web 状态 ────────────────────────────────
    from paper_trading.app import update_paper_state
    update_paper_state(report)
    print("  Web 状态已同步")

    # ── 4. 启动 ────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  📊 http://<server-ip>:{args.port}")
    print(f"  按 Ctrl+C 退出")
    print(f"{'='*60}\n")

    from paper_trading.app import app
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == '__main__':
    main()
