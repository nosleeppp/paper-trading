#!/usr/bin/env python3
"""
ICIR_monthly 实盘模拟 — 初始化启动脚本
=======================================
读取 config.json，执行首次建仓，启动 Web 可视化面板。

用法:
  cd /root/lqq_bot_workspace/ICIR_monthly
  venv/bin/python scripts/bootstrap.py                # 使用 config.json 默认配置
  venv/bin/python scripts/bootstrap.py --signal signals/custom.json  # 指定信号文件
  venv/bin/python scripts/bootstrap.py --date 20250630 --capital 50000000
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# ═══════════════════════════════════════════════════════════════════════
# 路径设置
# ═══════════════════════════════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)  # ICIR_monthly/

# 将策略模块的 collab_root 加入 sys.path（用于 import 回测策略）
CONFIG_PATH = os.path.join(ROOT_DIR, 'config.json')


def load_config() -> dict:
    """加载配置文件。"""
    if not os.path.exists(CONFIG_PATH):
        print(f"[Bootstrap] 未找到配置文件: {CONFIG_PATH}")
        print("[Bootstrap] 使用默认配置")
        return {}

    with open(CONFIG_PATH, 'r') as f:
        cfg = json.load(f)

    # 去除注释字段
    cfg.pop('_comment', None)
    return cfg


def load_targets(signal_file: str) -> list:
    """从信号文件加载目标股票池。"""
    if not signal_file or not os.path.exists(signal_file):
        return []

    try:
        with open(signal_file, 'r') as f:
            data = json.load(f)
        targets = data.get('targets', [])
        if targets:
            print(f"[Bootstrap] 从信号文件加载 {len(targets)} 只标的")
            return targets
    except Exception as e:
        print(f"[Bootstrap] 信号文件加载失败: {e}")

    return []


def generate_signal_from_backtest(cfg: dict, target_date: str) -> list:
    """
    尝试调用 quant_backtest 在线生成信号。
    成功返回股票列表，失败返回空列表。
    """
    strategy_cfg = cfg.get('strategy', {})
    collab_root = strategy_cfg.get('collab_root', '/root/lqq_bot_workspace')
    module_name = strategy_cfg.get('module', 'IC5_T100_ICIRTM10_ICIRN20_月频_218因子')

    if collab_root not in sys.path:
        sys.path.insert(0, collab_root)

    try:
        from quant_backtest import DataCache
        import importlib
        mod = importlib.import_module(module_name)
        StrategyClass = None
        for name in dir(mod):
            obj = getattr(mod, name)
            if (isinstance(obj, type) and
                hasattr(obj, 'initialize') and
                hasattr(obj, 'schedule_handler') and
                hasattr(obj, 'NAME')):
                StrategyClass = obj
                break

        if StrategyClass is None:
            raise ValueError("未找到策略类")

        data_dir = cfg.get('data_dir', '/root/lqq_bot_workspace/data')
        strategy = StrategyClass(data_dir=data_dir)
        data_cache = DataCache(data_dir=data_dir, duckdb_file='data.duckdb')

        # 预加载因子缓存
        start_str = f'{int(target_date[:4]) - 1}0101'
        end_str = f'{target_date[:4]}1231'
        factor_df = strategy._load_factor_chunk(data_cache, start_str, end_str)
        if factor_df is not None and not factor_df.empty:
            strategy._factor_cache = {
                str(d): grp.drop(columns=['trade_date', 'ts_code'], errors='ignore')
                for d, grp in factor_df.groupby('trade_date')
            }

        # 创建 mock context 并调用 _on_select
        class MockBacktester:
            def __init__(self, dc): self._data_cache = dc
        class MockContext:
            def __init__(self, dt, dc):
                self.current_dt = dt
                self._backtester = MockBacktester(dc)

        mock_ctx = MockContext(target_date, data_cache)
        strategy._on_select(mock_ctx)

        cached = strategy._select_cache.get(target_date)
        if cached:
            if isinstance(cached, tuple):
                targets = list(cached[0])
            else:
                targets = list(cached)
            print(f"[Bootstrap] 在线生成信号: {len(targets)} 只标的")
            return targets

    except ImportError:
        print("[Bootstrap] quant_backtest 未安装，跳过在线信号生成")
    except Exception as e:
        print(f"[Bootstrap] 在线信号生成失败: {e}")
        import traceback
        traceback.print_exc()

    return []


def execute_initial_rebalance(targets: list, capital: float, start_date: str):
    """执行首次等权建仓，返回 report dict。"""
    from paper_trading.broker import PaperBroker, BrokerConfig
    from paper_trading.qmt_compat import OP_BUY, ORDER_MARKET, TickData
    from paper_trading.data_provider import SinaDataProvider

    config = BrokerConfig(initial_capital=capital)
    broker = PaperBroker(config)
    broker.current_date = start_date
    broker.current_time = '09:30:00'

    # 获取实时行情
    print("[Bootstrap] 获取实时行情...")
    provider = SinaDataProvider()
    ticks = provider.get_ticks_batch(targets)
    limit_info = provider.get_limit_info(start_date)
    broker.update_market_data(ticks, limit_info)

    # 对无行情标的用默认价格
    for code in targets:
        if code not in ticks:
            tick = TickData(stockcode=code, last_price=10.0, bid1=9.99, ask1=10.01)
            ticks[code] = tick
    broker.update_market_data(ticks, limit_info)

    # 等权建仓
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

    # CLI 参数覆盖配置文件
    parser = argparse.ArgumentParser(description='ICIR_monthly 初始化')
    parser.add_argument('--date', '-d', default=cfg.get('start_date', '20250529'))
    parser.add_argument('--signal', '-s', default=cfg.get('signal_file'))
    parser.add_argument('--capital', '-c', type=float,
                        default=cfg.get('capital', 100_000_000))
    parser.add_argument('--port', '-p', type=int,
                        default=cfg.get('web', {}).get('port', 8899))
    parser.add_argument('--host', default=cfg.get('web', {}).get('host', '0.0.0.0'))
    parser.add_argument('--auto-signal', action='store_true',
                        help='自动调用 quant_backtest 在线生成信号')
    args = parser.parse_args()

    start_date = args.date
    capital = args.capital
    signal_file = args.signal
    # 解析相对路径
    if signal_file and not os.path.isabs(signal_file):
        signal_file = os.path.join(ROOT_DIR, signal_file)

    print(f"\n{'='*60}")
    print(f"  ICIR_monthly 实盘模拟初始化")
    print(f"  建仓日: {start_date}")
    print(f"  初始资金: {capital:,.0f}")
    print(f"{'='*60}\n")

    # ── 1. 加载目标池 ─────────────────────────────────
    targets = load_targets(signal_file) if signal_file else []

    if not targets and args.auto_signal:
        print("[Bootstrap] 尝试在线生成信号...")
        targets = generate_signal_from_backtest(cfg, start_date)

    if not targets:
        # 回退：在 config 中配置的默认标的
        fallback = cfg.get('default_targets', [])
        if fallback:
            targets = fallback
            print(f"[Bootstrap] 使用 config.json 中的默认股票池: {len(targets)} 只")
        else:
            print("[Bootstrap] 错误: 无可用标的，请提供 --signal 或配置 default_targets")
            sys.exit(1)

    # 保存信号到信号文件
    output_dir = cfg.get('output_dir', os.path.join(ROOT_DIR, 'signals'))
    os.makedirs(output_dir, exist_ok=True)
    out_signal = os.path.join(output_dir, 'paper_signal_targets.json')
    with open(out_signal, 'w') as f:
        json.dump({'date': start_date, 'targets': targets,
                    'generated_at': __import__('datetime').datetime.now().isoformat()},
                   f, ensure_ascii=False)
    print(f"[Bootstrap] 信号已保存: {out_signal}")

    # ── 2. 执行首次建仓 ───────────────────────────────
    report = execute_initial_rebalance(targets, capital, start_date)

    bought = len(report['trades'])
    print(f"\n  成交: {bought}/{len(targets)} 只")
    print(f"  总资产: {report['total_value']:,.2f}")
    print(f"  可用资金: {report['cash']:,.2f}")

    # ── 3. 写入 Web 状态 ──────────────────────────────
    from paper_trading.app import update_paper_state
    update_paper_state(report)
    print("  Web 状态已同步")

    # ── 4. 启动 Web 面板 ──────────────────────────────
    print(f"\n{'='*60}")
    print(f"  📊 Web 面板启动")
    print(f"  访问: http://<server-ip>:{args.port}")
    print(f"  按 Ctrl+C 退出")
    print(f"{'='*60}\n")

    from paper_trading.app import app
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == '__main__':
    main()
