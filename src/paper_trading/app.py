"""
实盘模拟系统 - Web 监控面板 (Flask)
===================================
实时查看账户状态、持仓、成交记录、净值曲线。
支持回测结果展示：在线运行 / 导入 quant_backtest 输出文件。

启动:
    paper-trading web
    → http://127.0.0.1:8899
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, jsonify, request

_PKG_DIR = os.path.dirname(__file__)

# ═══════════════════════════════════════════════════════════════════════
# 全局共享状态
# ═══════════════════════════════════════════════════════════════════════

_paper_state: Dict[str, Any] = {
    "account": {"capital": 0, "total_value": 0, "total_return": 0, "position_count": 0},
    "positions": [], "orders": [], "pnl_curve": [], "intraday": [],
    "last_update": None,
}

_backtest_tasks: Dict[str, Dict[str, Any]] = {}
_backtest_task_counter = 0

# 实时更新配置
_realtime_store: Any = None         # PaperStore 实例，由 bootstrap 注入
_realtime_targets: List[str] = []   # 持仓标的列表
_realtime_thread: Optional[threading.Thread] = None
_realtime_running = False


def update_paper_state(report: dict):
    """由 engine 或 bootstrap 调用，写入实盘状态。"""
    _paper_state["account"] = {
        "capital": report.get("cash", 0),
        "total_value": report.get("total_value", 0),
        "total_return": report.get("total_return", 0),
        "initial_capital": report.get("initial_capital", 0),
        "position_count": len(report.get("positions", {})),
    }
    _paper_state["positions"] = [
        {
            "stockcode": code,
            "quantity": p.get("quantity", 0),
            "avg_cost": p.get("avg_cost", 0),
            "price": p.get("market_value", 0) / max(p.get("quantity", 1), 1),
            "market_value": p.get("market_value", 0),
            "unrealized_pnl": p.get("unrealized_pnl", 0),
            "return_rate": (p.get("market_value", 0) / max(p.get("quantity", 1), 1) - p.get("avg_cost", 0))
                           / max(p.get("avg_cost", 0.01), 0.01),
        }
        for code, p in report.get("positions", {}).items()
    ]
    _paper_state["orders"] = [
        {"time": t.get("time", ""), "stockcode": t.get("stockcode", ""),
         "side": t.get("side", "BUY"), "quantity": t.get("quantity", 0),
         "price": t.get("price", 0),
         "amount": t.get("quantity", 0) * t.get("price", 0)}
        for t in report.get("trades", [])
    ]
    # 净值曲线 — 优先从 nav_series 构建（完整历史），回退到 minute_snapshots
    nav_src = report.get('nav_series', [])
    if nav_src:
        _paper_state["pnl_curve"] = [
            {"date": n.get("date", ""), "nav": n.get("nav", 1)}
            for n in nav_src
        ]
    else:
        init_cap = report.get("initial_capital", 1)
        _paper_state["pnl_curve"] = [
            {"date": s.get("time", ""),
             "nav": s.get("total_value", 0) / max(init_cap, 1)}
            for s in report.get("minute_snapshots", [])
        ]
    _paper_state["intraday"] = report.get("minute_snapshots", [])
    _paper_state["last_update"] = datetime.now().isoformat()
    # 基准净值（如有）
    bench = report.get("benchmark_nav", [])
    if bench:
        _paper_state["benchmark_nav"] = bench


# ═══════════════════════════════════════════════════════════════════════
# 异步回测 runner
# ═══════════════════════════════════════════════════════════════════════

def _load_strategy_module(module_name: str, pythonpath: str = None):
    """加载策略模块（支持文件路径 / dotted path）。"""
    import importlib.util as _util

    mod = None
    if module_name.endswith('.py'):
        if os.path.isabs(module_name) and os.path.exists(module_name):
            spec = _util.spec_from_file_location('_bt_strategy', module_name)
            mod = _util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        elif pythonpath:
            fpath = os.path.join(pythonpath, module_name)
            if os.path.exists(fpath):
                spec = _util.spec_from_file_location('_bt_strategy', fpath)
                mod = _util.module_from_spec(spec)
                spec.loader.exec_module(mod)

    if mod is None:
        try:
            mod = __import__(module_name, fromlist=['*'])
        except ImportError:
            pass

    if mod is None:
        raise ImportError(
            f"无法加载策略模块: {module_name}\n"
            f"  PYTHONPATH={pythonpath}\n"
            "  请确认文件路径正确，或使用「导入结果」模式。"
        )
    return mod


def _run_backtest_async(task_id: str, start_date: str, end_date: str,
                        strategy_module: str = None, capital: float = 100_000_000,
                        pythonpath: str = None, data_dir: str = None):
    """后台线程：运行 quant_backtest 回测。"""
    try:
        _backtest_tasks[task_id]["status"] = "running"

        # 将 pythonpath 及下级 site-packages 加入搜索路径
        if pythonpath:
            import sys
            for p in pythonpath.split(":"):
                p = p.strip()
                if not p:
                    continue
                if p not in sys.path:
                    sys.path.insert(0, p)
                # 同时搜索 pythonpath 下的 site-packages
                for sub in ('lib', 'lib64'):
                    sp = os.path.join(p, sub)
                    for entry in os.listdir(sp) if os.path.isdir(sp) else []:
                        if entry.startswith('python') and 'site-packages' in os.listdir(os.path.join(sp, entry)):
                            spp = os.path.join(sp, entry, 'site-packages')
                            if spp not in sys.path:
                                sys.path.insert(0, spp)

        # 尝试导入 quant_backtest
        try:
            from quant_backtest import Backtester, DataCache
        except ImportError as e:
            import sys, importlib.util
            spec = importlib.util.find_spec('quant_backtest')
            found_at = spec.origin if spec else 'NOT FOUND'
            raise ImportError(
                f"无法导入 quant_backtest.Backtester\n"
                f"  quant_backtest 位置: {found_at}\n"
                f"  sys.path 前5项: {sys.path[:5]}\n"
                f"  请确认 PYTHONPATH 指向 quant_backtest 所在目录,\n"
                f"  并在该目录的 venv 中安装 paper_trading,\n"
                f"  或使用「导入结果」模式上传回测输出文件。"
            ) from e

        # 全部路径从环境变量或请求参数获取
        data_dir = data_dir or os.environ.get('PAPER_DATA_DIR', '')
        duckdb_path = os.environ.get('PAPER_DUCKDB_PATH',
                                      os.path.join(data_dir, 'data.duckdb'))
        output_dir = os.environ.get('PAPER_OUTPUT_DIR',
                                     os.path.join(os.path.dirname(data_dir), 'output'))
        if not data_dir:
            raise ValueError("未设置 data_dir。请通过请求参数或环境变量 PAPER_DATA_DIR 指定。")

        data_cache = DataCache(data_dir=data_dir, duckdb_file=os.path.basename(duckdb_path))

        # ── 加载策略模块 ──
        if not strategy_module:
            raise ValueError(
                "未指定策略模块。请在「策略模块路径」中填写策略文件的路径。"
            )

        mod = _load_strategy_module(strategy_module, pythonpath)

        # 查策略类 — 排除基类/配置类，取覆盖了 _load_factor_chunk 的最深层子类
        from quant_backtest.strategies import FactorStrategyTemplate
        base_load = FactorStrategyTemplate._load_factor_chunk
        candidates = []
        for name in dir(mod):
            obj = getattr(mod, name)
            if not isinstance(obj, type) or not hasattr(obj, 'NAME'):
                continue
            if obj is FactorStrategyTemplate:
                continue
            try:
                if not issubclass(obj, FactorStrategyTemplate):
                    continue
            except TypeError:
                continue
            if getattr(obj, '_load_factor_chunk', base_load) is base_load:
                continue
            candidates.append(obj)

        StrategyClass = max(candidates, key=lambda c: len(c.__mro__)) if candidates else None

        if StrategyClass is None:
            raise ValueError(
                f"在 {strategy_module} 中未找到策略类\n"
                "策略类需有 initialize, schedule_handler, NAME 属性"
            )

        strategy = StrategyClass(data_dir=data_dir)

        # ── 直接调用策略方法生成信号（不依赖 Backtester / factors_all 表）──
        year_s, year_e = int(start_date[:4]), int(end_date[:4])
        factor_df = strategy._load_factor_chunk(
            data_cache, f'{year_s - 1}0101', f'{year_e}1231'
        )
        if factor_df is None or factor_df.empty:
            raise RuntimeError(
                f"因子数据为空 ({year_s - 1}~{year_e})。\n"
                f"请检查 data_dir={data_dir} 中 factors_YYYY 年度表是否存在。"
            )

        # 填充 _factor_cache → _on_select → 提取信号
        factor_cols = [c for c in factor_df.columns if c not in ('trade_date', 'ts_code')]
        strategy._factor_cache = {}
        for d, grp in factor_df.groupby('trade_date'):
            strategy._factor_cache[str(d)] = grp.set_index('ts_code')[factor_cols]

        # 模拟 context（只给 _on_select → _select 需要的属性）
        class _BTMock:
            def __init__(self, dc): self._data_cache = dc
        ctx = type('_Ctx', (), {
            'current_dt': end_date,
            '_backtester': _BTMock(data_cache),
        })()

        # 对区间内每个交易日运行 _on_select，只取最后一天的结果
        strategy._on_select(ctx)
        targets = strategy._select_cache.get(end_date, [])
        if isinstance(targets, tuple):
            targets = list(targets[0])
        elif not isinstance(targets, list):
            targets = []

        if not targets:
            raise RuntimeError(f"_on_select({end_date}) 未产生信号")

        # 用真实 Backtester 跑完整回测获取 nav/trades（此时因子缓存已就绪）
        data_cache._precomputed_factor_cache = strategy._factor_cache
        bt = Backtester(
            start_date=start_date, end_date=end_date,
            initial_capital=capital, benchmark='000852.SH',
            commission_rate=0.0001, min_commission=5.0,
            slippage=0.001, stamp_duty_rate=0.001,
            price_type='qfq', data_dir=data_dir,
            strategy_name=f'backtest_{task_id}',
            output_dir=output_dir, output_charts=False,
            data_cache=data_cache,
        )
        bt.set_strategies(
            initialize=strategy.initialize,
            schedule_handler=strategy.schedule_handler,
        )
        bt.run()

        stats = getattr(bt, 'stats', {}) or {}
        nav_df = getattr(bt, 'nav_df', None)
        trades_df = getattr(bt, 'trades_df', None)

        result = {
            "account": {
                "total_return": stats.get("total_return", 0),
                "annual_return": stats.get("annual_return", 0),
                "sharpe": stats.get("sharpe_ratio", 0),
                "max_drawdown": stats.get("max_drawdown", 0),
                "win_rate": stats.get("win_rate", 0),
                "trade_count": stats.get("trade_count", 0),
                "alpha": stats.get("alpha", 0),
                "beta": stats.get("beta", 0),
            },
            "nav_series": [],
            "trades": [],
        }

        if nav_df is not None:
            import pandas as pd
            records = nav_df.to_dict(orient='records') if isinstance(nav_df, pd.DataFrame) else []
            result["nav_series"] = [
                {"date": str(r.get("date", "")),
                 "nav": float(r.get("nav", r.get("total_value", 0)))}
                for r in records
            ]

        if trades_df is not None:
            import pandas as pd
            records = trades_df.to_dict(orient='records') if isinstance(trades_df, pd.DataFrame) else []
            result["trades"] = [
                {"time": str(r.get("date", r.get("trade_date", ""))),
                 "stockcode": str(r.get("ts_code", "")),
                 "side": "BUY" if str(r.get("side", "")).upper() in ("BUY", "买入", "B") else "SELL",
                 "quantity": int(r.get("quantity", r.get("vol", 0))),
                 "price": float(r.get("price", 0))}
                for r in records
            ]

        _backtest_tasks[task_id]["status"] = "done"
        _backtest_tasks[task_id]["result"] = result

    except ImportError as e:
        _backtest_tasks[task_id]["status"] = "error"
        _backtest_tasks[task_id]["error"] = (
            f"quant_backtest 未安装或不在 PYTHONPATH 中: {e}\n"
            "请在「在线回测」表单中填写 PYTHONPATH 字段（如 /root/lqq_bot_workspace），\n"
            "或使用「导入结果」模式上传回测输出文件。"
        )
    except Exception:
        _backtest_tasks[task_id]["status"] = "error"
        _backtest_tasks[task_id]["error"] = traceback.format_exc()


# ═══════════════════════════════════════════════════════════════════════
# 回测结果文件解析
# ═══════════════════════════════════════════════════════════════════════

def _parse_trades_csv(content: bytes) -> list:
    """解析 交易记录.csv → trades 列表。"""
    import pandas as pd
    df = pd.read_csv(io.BytesIO(content), encoding='utf-8-sig')
    trades = []
    for _, row in df.iterrows():
        side = str(row.get('交易类型', '')).strip()
        trades.append({
            "time": str(row.get('日期', row.get('日期显示', ''))),
            "stockcode": str(row.get('证券代码', '')),
            "side": "BUY" if side == '买入' else "SELL",
            "quantity": int(row.get('成交量', 0)),
            "price": float(row.get('成交价', 0)),
        })
    return trades


def _parse_positions_csv(content: bytes) -> list:
    """解析 持仓记录.csv → 最新持仓列表（过滤 Cash 行）。"""
    import pandas as pd
    df = pd.read_csv(io.BytesIO(content), encoding='utf-8-sig')
    # 过滤现金行
    df = df[df['品种'] != 'Cash']
    if df.empty:
        return []
    # 取最后一个交易日的持仓
    last_date = df['日期'].max()
    latest = df[df['日期'] == last_date]
    positions = []
    for _, row in latest.iterrows():
        qty = int(re.sub(r'[^\d.\-]', '', str(row.get('数量', '0'))))
        price = float(re.sub(r'[^\d.\-]', '', str(row.get('收盘价/结算价', '0'))))
        positions.append({
            "stockcode": str(row.get('标的', '')),
            "quantity": qty,
            "avg_cost": float(row.get('开仓均价', 0)),
            "price": price,
            "market_value": float(row.get('市值/价值', 0)),
            "unrealized_pnl": float(row.get('盈亏/逐笔浮盈', 0)),
        })
    return positions


def _parse_nav_xlsx(content: bytes) -> list:
    """解析 净值序列.xlsx → nav_series。"""
    import pandas as pd
    df = pd.read_excel(io.BytesIO(content))
    nav_col = '策略净值' if '策略净值' in df.columns else df.columns[2]
    date_col = '日期' if '日期' in df.columns else df.columns[1]
    return [
        {"date": str(row[date_col]),
         "nav": float(row[nav_col])}
        for _, row in df.iterrows()
    ]


def _parse_summary_txt(content: bytes) -> dict:
    """解析 收益概述.txt → 完整指标 dict。"""
    text = content.decode('utf-8')
    result = {}

    def _pct(s):
        try: return float(s.replace('%','')) / 100
        except: return 0.0
    def _num(s):
        try: return float(s)
        except: return 0.0
    def _int(s):
        try: return int(s)
        except: return 0

    patterns = [
        # 收益指标
        ('total_return',      r'总收益率:\s*([\d\-.]+%)', _pct),
        ('annual_return',     r'年化收益率:\s*([\d\-.]+%)', _pct),
        ('benchmark_return',  r'基准收益率:\s*([\d\-.]+%)', _pct),
        ('excess_return',     r'超额收益率:\s*([\d\-.]+%)', _pct),
        ('annual_excess',     r'年化超额收益:\s*([\d\-.]+%)', _pct),
        ('alpha',             r'Alpha:\s*([\d\-.]+)', _num),
        ('beta',              r'Beta:\s*([\d\-.]+)', _num),
        # 风险指标
        ('volatility',        r'年化波动率:\s*([\d\-.]+%)', _pct),
        ('sharpe',            r'夏普比率:\s*([\d\-.]+)', _num),
        ('max_drawdown',      r'最大回撤:\s*([\d\-.]+%)', _pct),
        ('tracking_error',    r'跟踪误差:\s*([\d\-.]+%)', _pct),
        ('information_ratio', r'信息比率:\s*([\d\-.]+)', _num),
        ('excess_sharpe',     r'超额夏普:\s*([\d\-.]+)', _num),
        ('excess_max_dd',     r'超额最大回撤:\s*([\d\-.]+%)', _pct),
        # 交易统计
        ('win_rate',          r'胜率:\s*([\d\-.]+%)', _pct),
        ('profit_loss_ratio', r'盈亏比:\s*([\d\-.]+)', _num),
        ('trade_count',       r'交易次数:\s*(\d+)', _int),
        # 区间
        ('start_date',        r'回测区间:\s*(\d{8})\s*~', str),
        ('end_date',          r'~\s*(\d{8})', str),
    ]

    for key, pattern, converter in patterns:
        m = re.search(pattern, text)
        if m:
            result[key] = converter(m.group(1))

    return result


# ═══════════════════════════════════════════════════════════════════════
# Flask app
# ═══════════════════════════════════════════════════════════════════════

def _load_backtest_folder(folder: str) -> dict:
    """加载一个回测结果文件夹，返回统一格式。"""
    import pandas as pd

    result = {'account': {}, 'nav_series': [], 'trades': [], 'positions': []}

    # 交易记录
    trades_path = os.path.join(folder, '交易记录.csv')
    if os.path.exists(trades_path):
        df = pd.read_csv(trades_path, encoding='utf-8-sig')
        for _, r in df.iterrows():
            result['trades'].append({
                'time': str(r['日期']),
                'stockcode': str(r['证券代码']),
                'side': 'BUY' if str(r.get('交易类型', '')).strip() == '买入' else 'SELL',
                'quantity': int(r['成交量']),
                'price': float(r['成交价']),
            })

    # 净值序列
    nav_path = os.path.join(folder, '净值序列.xlsx')
    if os.path.exists(nav_path):
        df = pd.read_excel(nav_path)
        nav_col = '策略净值' if '策略净值' in df.columns else df.columns[2]
        date_col = df.columns[1]
        tv_col = '总资产' if '总资产' in df.columns else df.columns[3]
        for _, r in df.iterrows():
            result['nav_series'].append({
                'date': str(r[date_col])[:10].replace('/', '').replace('-', ''),
                'nav': float(r[nav_col]),
                'total_value': float(r[tv_col]) if tv_col in df.columns else 0,
            })
        if not result['nav_series'].empty if hasattr(result['nav_series'], 'empty') else True:
            last = result['nav_series'][-1] if result['nav_series'] else {}
            first = result['nav_series'][0] if result['nav_series'] else {}
            result['account']['total_return'] = (last.get('nav', 1) - 1.0) if last else 0

    # 持仓记录
    pos_path = os.path.join(folder, '持仓记录.csv')
    if os.path.exists(pos_path):
        pdf = pd.read_csv(pos_path, encoding='utf-8-sig')
        pdf = pdf[pdf['品种'] != 'Cash']
        if not pdf.empty:
            last_date = pdf['日期'].max()
            latest = pdf[pdf['日期'] == last_date]
            for _, r in latest.iterrows():
                code = str(r['标的'])
                qty = int(float(str(r['数量']).replace('股', '')))
                avg_cost = float(r['开仓均价'])
                price = float(r['收盘价/结算价'])
                mv = float(r['市值/价值'])
                pnl = float(r['盈亏/逐笔浮盈'])
                result['positions'].append({
                    'stockcode': code, 'quantity': qty,
                    'avg_cost': avg_cost, 'price': price,
                    'market_value': mv, 'unrealized_pnl': pnl,
                    'return_rate': (price - avg_cost) / max(avg_cost, 0.01),
                })

    # 收益概述
    summary_path = os.path.join(folder, '收益概述.txt')
    if os.path.exists(summary_path):
        result['account'].update(_parse_summary_txt(open(summary_path, 'rb').read()))

    # 回撤
    result['drawdown_series'] = _calc_drawdown(result['nav_series'])

    # 基准净值
    idx_path = os.environ.get('PAPER_INDEX_DAILY_PATH', '')
    if idx_path and os.path.exists(idx_path):
        try:
            import pandas as pd
            idx_df = pd.read_parquet(idx_path)
            code_col = next((c for c in ['ts_code', 'code'] if c in idx_df.columns), idx_df.columns[1])
            date_col = next((c for c in ['trade_date', 'date'] if c in idx_df.columns), idx_df.columns[0])
            close_col = next((c for c in ['close', 'qfq_close'] if c in idx_df.columns), idx_df.columns[-1])
            bench = idx_df[idx_df[code_col].astype(str).str.replace('.SH','').str.replace('.SZ','') == '000852']
            if not bench.empty:
                bench = bench.sort_values(date_col)
                bench['ds'] = bench[date_col].astype(str).str[:8]
                # 以 nav_series 第一个日期对应的基准价格为 1.0，确保起点对齐
                first_strategy_date = result['nav_series'][0]['date'] if result['nav_series'] else ''
                base_row = bench[bench['ds'] == first_strategy_date]
                if not base_row.empty:
                    base_price = float(base_row.iloc[0][close_col])
                else:
                    base_price = float(bench.iloc[0][close_col])
                if base_price > 0:
                    result['benchmark_nav'] = [
                        {'date': str(r[date_col])[:10].replace('-','').replace('/',''),
                         'nav': float(r[close_col]) / base_price}
                        for _, r in bench.iterrows()
                    ]
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════════════════
# 完全复刻 quant_backtest 的绩效指标计算（不依赖 quant_backtest 包）
# ═══════════════════════════════════════════════════════════════════════

def _calc_trade_stats(trades_df):
    """复刻 quant_backtest._calculate_trade_stats"""
    import numpy as np
    if trades_df is None or trades_df.empty:
        return {'total_trades': 0, 'win_rate': 0.0, 'profit_loss_ratio': 0.0}

    trades = trades_df.copy()
    total_trades = len(trades)
    profits, losses = [], []

    if 'ts_code' in trades.columns and 'side' in trades.columns:
        for ts_code in trades['ts_code'].unique():
            stock_trades = trades[trades['ts_code'] == ts_code].sort_values('date')
            buy_queue = []
            for _, trade in stock_trades.iterrows():
                action = str(trade.get('side', ''))
                price = float(trade.get('price', 0))
                qty = int(trade.get('quantity', 0))
                commission = float(trade.get('commission', 0))
                tax = float(trade.get('tax', 0))
                if action.upper() == 'BUY':
                    buy_queue.append((price, qty, commission))
                elif action.upper() == 'SELL':
                    sell_qty = qty
                    sell_price = price
                    total_cost, total_qty = 0.0, 0
                    while sell_qty > 0 and buy_queue:
                        buy_price, buy_qty, buy_comm = buy_queue[0]
                        if buy_qty <= sell_qty:
                            total_cost += buy_price * buy_qty + buy_comm
                            total_qty += buy_qty
                            sell_qty -= buy_qty
                            buy_queue.pop(0)
                        else:
                            total_cost += buy_price * sell_qty + buy_comm * (sell_qty / buy_qty)
                            total_qty += sell_qty
                            buy_queue[0] = (buy_price, buy_qty - sell_qty,
                                            buy_comm * (buy_qty - sell_qty) / buy_qty)
                            sell_qty = 0
                    if total_qty > 0:
                        avg_cost = total_cost / total_qty
                        pnl = sell_price * total_qty - total_cost - commission - tax
                        if pnl > 0:
                            profits.append(pnl)
                        elif pnl < 0:
                            losses.append(abs(pnl))

    win_count = len(profits)
    loss_count = len(losses)
    total_closed = win_count + loss_count
    win_rate = win_count / total_closed if total_closed > 0 else 0.0
    avg_profit = np.mean(profits) if profits else 0
    avg_loss = np.mean(losses) if losses else 0
    profit_loss_ratio = avg_profit / avg_loss if avg_loss != 0 else 0.0
    return {'total_trades': total_trades, 'win_rate': win_rate, 'profit_loss_ratio': profit_loss_ratio}


def _calc_benchmark_metrics(daily_df, benchmark_data, strategy_daily_returns, risk_free_rate):
    """复刻 quant_backtest._calculate_benchmark_metrics"""
    import numpy as np
    import pandas as pd
    from datetime import datetime as _dt

    # 策略日期（兼容 YYYYMMDD 和 YYYY-MM-DD）
    date_strs = daily_df['date'].astype(str).str.replace('-', '')
    sd = pd.to_datetime(date_strs, format='%Y%m%d')
    strategy_dates = sd.dt.normalize() if hasattr(sd, 'dt') else pd.DatetimeIndex(sd).normalize()
    strategy_values = daily_df['total_value'].values
    strategy_nav = pd.Series(strategy_values, index=strategy_dates)
    strategy_nav = strategy_nav / strategy_nav.iloc[0]

    # 基准日期（兼容 YYYYMMDD 和 YYYY-MM-DD）
    bd_strs = benchmark_data['trade_date'].astype(str).str.replace('-', '')
    bd = pd.to_datetime(bd_strs, format='%Y%m%d')
    bench_dates = bd.dt.normalize() if hasattr(bd, 'dt') else pd.DatetimeIndex(bd).normalize()
    if 'nav' in benchmark_data.columns:
        bench_nav = pd.Series(benchmark_data['nav'].values, index=bench_dates)
    else:
        bench_nav = pd.Series(benchmark_data['close'].values, index=bench_dates)
        # 以 common 交集第一天的 close 为基准归一化（与策略对齐），不是 iloc[0]
        bench_nav = bench_nav / bench_nav.iloc[0]

    # 对齐
    common = strategy_nav.index.intersection(bench_nav.index)
    if len(common) == 0:
        return {}
    s_nav = strategy_nav.loc[common]
    b_nav = bench_nav.loc[common]
    # 以公共交集第一天的 close 为基准归一化（确保基准和策略同日起始=1.0）
    if b_nav.iloc[0] > 0:
        b_nav = b_nav / b_nav.iloc[0]
    total_days = len(common)

    strategy_return = s_nav.iloc[-1] - 1
    benchmark_return = b_nav.iloc[-1] - 1
    strategy_annual_return = (1 + strategy_return) ** (252 / total_days) - 1 if total_days > 0 else 0
    benchmark_annual_return = (1 + benchmark_return) ** (252 / total_days) - 1 if total_days > 0 else 0
    excess_return = s_nav.iloc[-1] / b_nav.iloc[-1] - 1
    excess_annual_return = (1 + excess_return) ** (252 / total_days) - 1 if total_days > 0 else 0

    # 相对净值
    relative_nav = s_nav / b_nav
    cumulative_excess = relative_nav - 1
    daily_excess_returns = relative_nav.pct_change().dropna()

    # Alpha/Beta
    rets_df = pd.DataFrame({'strategy': s_nav, 'benchmark': b_nav}).pct_change().dropna()
    s_rets = rets_df['strategy']
    b_rets = rets_df['benchmark']
    if len(s_rets) > 1 and len(b_rets) > 1:
        cov = np.cov(s_rets, b_rets)[0, 1]
        b_var = np.var(b_rets)
        beta = cov / b_var if b_var != 0 else 1.0
    else:
        beta = 1.0
    alpha = strategy_annual_return - (risk_free_rate + beta * (benchmark_annual_return - risk_free_rate))

    # 跟踪误差 / 信息比率
    tracking_error = daily_excess_returns.std() * np.sqrt(252)
    information_ratio = excess_annual_return / tracking_error if tracking_error != 0 else 0.0

    # 超额最大回撤
    excess_cummax = cumulative_excess.cummax()
    excess_drawdown = (cumulative_excess - excess_cummax) / (1 + excess_cummax)
    max_excess_drawdown = float(excess_drawdown.min())

    # 超额夏普
    excess_vol = daily_excess_returns.std() * np.sqrt(252)
    excess_sharpe = excess_annual_return / excess_vol if excess_vol != 0 else 0.0

    return {
        'strategy_return': strategy_return,
        'benchmark_return': benchmark_return,
        'strategy_annual_return': strategy_annual_return,
        'benchmark_annual_return': benchmark_annual_return,
        'excess_return': excess_return,
        'excess_annual_return': excess_annual_return,
        'alpha': float(alpha), 'beta': float(beta),
        'tracking_error': float(tracking_error),
        'information_ratio': float(information_ratio),
        'max_excess_drawdown': float(max_excess_drawdown),
        'excess_sharpe_ratio': float(excess_sharpe),
    }


def _calc_performance_metrics(daily_df, trades_df=None, benchmark_data=None, risk_free_rate=0.018):
    """复刻 quant_backtest.calculate_performance_metrics"""
    import pandas as pd
    import numpy as np

    if daily_df.empty or 'total_value' not in daily_df.columns:
        return {}

    df = daily_df.copy()
    df['total_value'] = df['total_value'].ffill().bfill()

    total_days = len(df)
    initial_value = float(df['total_value'].iloc[0])
    final_value = float(df['total_value'].iloc[-1])

    if pd.isna(initial_value) or pd.isna(final_value) or initial_value <= 0:
        return {
            'analysis_period_days': total_days,
            'initial_capital': initial_value if not pd.isna(initial_value) else 0,
            'final_capital': final_value if not pd.isna(final_value) else 0,
            'total_return': 0.0, 'annual_return': 0.0,
            'annual_volatility': 0.0, 'max_drawdown': 0.0,
            'sharpe_ratio': 0.0, 'total_trades': 0,
            'win_rate': 0.0, 'profit_loss_ratio': 0.0,
        }

    strategy_daily_returns = df['total_value'].pct_change().dropna()
    total_return = (final_value - initial_value) / initial_value
    annual_return = (1 + total_return) ** (252 / total_days) - 1 if total_days > 0 else 0
    annual_volatility = float(strategy_daily_returns.std() * np.sqrt(252)) if len(strategy_daily_returns) > 0 else 0

    cummax = df['total_value'].cummax()
    drawdown = (df['total_value'] - cummax) / cummax
    max_drawdown = float(drawdown.min()) if len(drawdown) > 0 else 0

    if annual_volatility != 0 and not np.isnan(annual_volatility) and not np.isinf(annual_volatility):
        sharpe_ratio = (annual_return - risk_free_rate) / annual_volatility
    else:
        sharpe_ratio = 0.0

    for v in ['total_return', 'annual_return', 'annual_volatility', 'max_drawdown', 'sharpe_ratio']:
        val = locals()[v]
        if np.isnan(val) or np.isinf(val):
            locals()[v] = 0.0

    trade_stats = _calc_trade_stats(trades_df)
    benchmark_metrics = {}
    if benchmark_data is not None and not benchmark_data.empty:
        benchmark_metrics = _calc_benchmark_metrics(
            daily_df, benchmark_data, strategy_daily_returns, risk_free_rate
        )

    metrics = {
        'analysis_period_days': total_days,
        'initial_capital': initial_value,
        'final_capital': final_value,
        'total_return': total_return,
        'annual_return': annual_return,
        'annual_volatility': annual_volatility,
        'max_drawdown': max_drawdown,
        'sharpe_ratio': sharpe_ratio,
        **trade_stats,
        **benchmark_metrics,
    }
    return metrics


def _resolve_index_daily_path() -> str:
    """多级回退搜索 index_daily.parquet。"""
    # 1. 环境变量
    p = os.environ.get('PAPER_INDEX_DAILY_PATH', '')
    if p and os.path.exists(p):
        return p
    # 2. data_dir 下级
    data_dir = os.environ.get('PAPER_DATA_DIR', '')
    if data_dir:
        for sub in ('index_daily/index_daily.parquet', 'index_daily.parquet'):
            p = os.path.join(data_dir, sub)
            if os.path.exists(p):
                return p
    # 3. 常见路径
    for p in ('/root/lqq_bot_workspace/data/index_daily/index_daily.parquet',
              '/root/lqq_bot_workspace/data/index_daily.parquet'):
        if os.path.exists(p):
            return p
    return ''


def _compute_paper_metrics() -> dict:
    """从 DB 读取数据，用复刻的 _calc_performance_metrics 计算实盘指标。"""
    import pandas as pd
    nav_rows = _realtime_store._get_conn().execute(
        "SELECT date, nav, total_value, cash FROM nav_series ORDER BY date"
    ).fetchall()
    if not nav_rows:
        return {}

    daily_df = pd.DataFrame(nav_rows, columns=['date', 'nav', 'total_value', 'cash'])
    daily_df['date'] = daily_df['date'].astype(str).str.replace('-', '')

    order_rows = _realtime_store._get_conn().execute(
        "SELECT trade_date as date, stockcode as ts_code, side, quantity, price, 0 as commission, 0 as tax "
        "FROM orders ORDER BY id"
    ).fetchall()
    trades_df = pd.DataFrame(order_rows, columns=[
        'date', 'ts_code', 'side', 'quantity', 'price', 'commission', 'tax'
    ]) if order_rows else None

    benchmark_data = None
    idx_path = _resolve_index_daily_path()
    if idx_path:
        import pandas as _pd
        idx_df = _pd.read_parquet(idx_path)
        code_col = next((c for c in ['ts_code','code'] if c in idx_df.columns), idx_df.columns[1])
        idx_df = idx_df[idx_df[code_col].astype(str).str.replace('.SH','').str.replace('.SZ','') == '000852']
        if not idx_df.empty:
            date_col = next((c for c in ['trade_date','date'] if c in idx_df.columns), idx_df.columns[0])
            close_col = next((c for c in ['close','qfq_close'] if c in idx_df.columns), idx_df.columns[-1])
            benchmark_data = idx_df[[date_col, close_col]].copy()
            benchmark_data.columns = ['trade_date', 'close']
            benchmark_data['trade_date'] = benchmark_data['trade_date'].astype(str).str.replace('-', '').str[:8]
            # 只保留策略日期范围内的基准数据
            strategy_dates = set(daily_df['date'].unique())
            benchmark_data = benchmark_data[benchmark_data['trade_date'].isin(strategy_dates)]

    # 合并实时基准：用 realtime loop 获取的当日价格
    live_bm = _paper_state.get('benchmark_nav', [])
    if live_bm and benchmark_data is not None and not benchmark_data.empty:
        import pandas as _pd
        benchmark_data['trade_date'] = benchmark_data['trade_date'].astype(str).str.replace('-', '').str[:8]
        for item in live_bm[-1:]:  # 只取最后一条（当日实时）
            d = str(item.get('date', '')).replace('-', '')[:8]
            if d and d not in set(benchmark_data['trade_date']):
                # 当日未在 parquet 中：用实时价格追加
                # 以 parquet 最后一天的 close 为基准，乘以实时 nav 反推 close
                last_close = float(benchmark_data.iloc[-1]['close'])
                bm_nav_val = item.get('nav', 0)
                if bm_nav_val > 0 and last_close > 0:
                    # 基准的 nav 已经是 close/strategy_start_close 归一化
                    # 所以 close = nav * strategy_start_close
                    # strategy_start_close = last_close / last_nav
                    # 但更简单：直接用实时价格
                    # 实时价格 = _bench_base_price * bm_nav_val
                    if _bench_base_price and _bench_base_price > 0:
                        today_close = _bench_base_price * bm_nav_val
                    else:
                        today_close = last_close * (bm_nav_val / (float(benchmark_data.iloc[-2]['close']) / _bench_base_price if len(benchmark_data) >= 2 and _bench_base_price else 1))
                        if today_close <= 0:
                            continue
                    benchmark_data = _pd.concat([
                        benchmark_data,
                        _pd.DataFrame([{'trade_date': d, 'close': today_close}])
                    ], ignore_index=True)

    return _calc_performance_metrics(daily_df, trades_df, benchmark_data)


def _register_routes(app):
    @app.route('/')
    def index():
        return render_template('index.html')

    # ── 实盘指标（用 quant_backtest 方法计算）─────────

    @app.route('/api/paper/metrics')
    def api_paper_metrics():
        """
        完全复刻 quant_backtest.calculate_performance_metrics 的计算逻辑。
        从 DB nav_series + index_daily.parquet 读取数据，用 numpy/pandas 计算。
        """
        try:
            import pandas as pd
            import numpy as np

            if not _realtime_store:
                return jsonify({})

            # ── 策略 daily_df ──
            nav_rows = _realtime_store._get_conn().execute(
                "SELECT date, nav, total_value, cash FROM nav_series ORDER BY date"
            ).fetchall()
            if not nav_rows:
                return jsonify({})

            daily_df = pd.DataFrame(nav_rows, columns=['date', 'nav', 'total_value', 'cash'])

            # ── 交易 trades_df ──
            order_rows = _realtime_store._get_conn().execute(
                "SELECT trade_date as date, stockcode as ts_code, side, quantity, price, 0 as commission, 0 as tax FROM orders ORDER BY id"
            ).fetchall()
            trades_df = pd.DataFrame(order_rows, columns=[
                'date', 'ts_code', 'side', 'quantity', 'price', 'commission', 'tax'
            ]) if order_rows else None

            # ── 基准 benchmark_data ──
            benchmark_data = None
            idx_path = os.environ.get('PAPER_INDEX_DAILY_PATH', '')
            if idx_path and os.path.exists(idx_path):
                idx_df = pd.read_parquet(idx_path)
                code_col = next((c for c in ['ts_code','code'] if c in idx_df.columns), idx_df.columns[1])
                idx_df = idx_df[idx_df[code_col].astype(str).str.replace('.SH','').str.replace('.SZ','') == '000852']
                if not idx_df.empty:
                    date_col = next((c for c in ['trade_date','date'] if c in idx_df.columns), idx_df.columns[0])
                    close_col = next((c for c in ['close','qfq_close'] if c in idx_df.columns), idx_df.columns[-1])
                    benchmark_data = idx_df[[date_col, close_col]].copy()
                    benchmark_data.columns = ['trade_date', 'close']
                    benchmark_data['trade_date'] = benchmark_data['trade_date'].astype(str).str[:8]

            # ── 调用复刻版 calculate_performance_metrics ──
            metrics = _calc_performance_metrics(daily_df, trades_df, benchmark_data)
            return jsonify(metrics)

        except Exception as e:
            return jsonify({'error': str(e)})

    # ── 实盘状态 ──────────────────────────────────────

    @app.route('/api/status')
    def api_status():
        metrics = {}
        metrics_error = None
        try:
            if _realtime_store:
                metrics = _compute_paper_metrics()
        except Exception as e:
            metrics_error = str(e)

        # 从 store 读取完整订单列表（包含 engine 后续成交）
        orders = list(_paper_state.get("orders", []))
        try:
            if _realtime_store:
                store_orders = _realtime_store.get_orders(limit=1000)
                if store_orders:
                    orders = store_orders
        except Exception:
            pass

        result = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime('%H:%M:%S'),
            "account": _paper_state.get("account", {}),
            "positions": _paper_state.get("positions", []),
            "orders": orders,
            "pnl_curve": _paper_state.get("pnl_curve", []),
            "benchmark_nav": _paper_state.get("benchmark_nav", []),
            "metrics": metrics,
        }
        if metrics_error:
            result["metrics_error"] = metrics_error
        return jsonify(result)

    @app.route('/api/paper/update', methods=['POST'])
    def api_paper_update():
        data = request.get_json(force=True)
        update_paper_state(data)
        return jsonify({"success": True})

    # ── 回测结果（本地文件夹加载）───────────────────────

    @app.route('/api/backtest/years')
    def api_backtest_years():
        """返回可用回测年份列表。"""
        base = os.environ.get('PAPER_BACKTEST_DIR', '')
        if not base or not os.path.isdir(base):
            return jsonify([])
        years = sorted([
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d)) and not d.startswith('.')
        ], key=lambda x: (x != 'FULL', x))
        return jsonify(years)

    @app.route('/api/backtest/load')
    def api_backtest_load():
        """加载指定年份的回测结果。"""
        year = request.args.get('year', 'FULL')
        base = os.environ.get('PAPER_BACKTEST_DIR', '')
        if not base:
            return jsonify({'error': 'PAPER_BACKTEST_DIR 未设置'}), 404

        folder = os.path.join(base, year)
        if not os.path.isdir(folder):
            return jsonify({'error': f'文件夹不存在: {folder}'}), 404

        try:
            result = _load_backtest_folder(folder)
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/backtest/list')
    def api_backtest_list():
        tasks = []
        for tid, t in _backtest_tasks.items():
            tasks.append({
                "task_id": tid, "status": t["status"],
                "has_result": t.get("result") is not None,
                "error": t.get("error"),
            })
        return jsonify({"tasks": tasks})


def _calc_drawdown(nav_series: list) -> list:
    """从净值序列计算回撤序列。"""
    if not nav_series:
        return []
    peak = nav_series[0]["nav"]
    dd_series = []
    for item in nav_series:
        nav = item["nav"]
        if nav > peak:
            peak = nav
        dd = (nav - peak) / peak if peak > 0 else 0
        dd_series.append({"date": item["date"], "drawdown": dd})
    return dd_series


# ═══════════════════════════════════════════════════════════════════════
# 实时更新循环
# ═══════════════════════════════════════════════════════════════════════

def start_realtime_updater(store, targets: List[str],
                           interval: int = 60, flush_interval: int = 300):
    """
    启动后台实时更新线程。
    - 每 interval 秒拉取实时价格，更新持仓市值/PNL
    - 每 flush_interval 秒持久化到 DB
    """
    global _realtime_store, _realtime_targets, _realtime_thread, _realtime_running

    _realtime_store = store
    _realtime_targets = list(targets)
    _realtime_running = True

    _realtime_thread = threading.Thread(
        target=_run_realtime_loop,
        args=(interval, flush_interval),
        name="paper-realtime",
        daemon=True,
    )
    _realtime_thread.start()
    logger = logging.getLogger(__name__)
    logger.info("[Realtime] 已启动 (interval=%ds, targets=%d)", interval, len(targets))


def stop_realtime_updater():
    """停止实时更新线程。"""
    global _realtime_running
    _realtime_running = False


def _run_realtime_loop(interval: int, flush_interval: int):
    """后台循环：拉价格 → 更新状态 → 写 DB → 每日追加净值。"""
    import time as _time
    from paper_trading.data_provider import SinaDataProvider

    logger = logging.getLogger(__name__)
    provider = SinaDataProvider()
    # 基准净值：从 store nav_series 第一天的 index_daily close 作为归一化基数
    _bench_base_price = None
    try:
        # 取策略起始日
        first_nav = _realtime_store._get_conn().execute(
            "SELECT date FROM nav_series ORDER BY date LIMIT 1"
        ).fetchone()
        strategy_start_date = str(first_nav[0]).replace('-', '')[:8] if first_nav else ''

        idx_path = _resolve_index_daily_path()
        if idx_path and strategy_start_date:
            import pandas as _pd
            _idf = _pd.read_parquet(idx_path)
            code_col = next((c for c in ['ts_code','code'] if c in _idf.columns), _idf.columns[1])
            date_col = next((c for c in ['trade_date','date'] if c in _idf.columns), _idf.columns[0])
            close_col = next((c for c in ['close','qfq_close'] if c in _idf.columns), _idf.columns[-1])
            _idf = _idf[_idf[code_col].astype(str).str.replace('.SH','').str.replace('.SZ','') == '000852']
            _idf['ds'] = _idf[date_col].astype(str).str[:8]
            start_row = _idf[_idf['ds'] == strategy_start_date]
            if not start_row.empty:
                _bench_base_price = float(start_row.iloc[0][close_col])
                logger.info("[Realtime] 基准基数: %.2f (策略起始日 %s)", _bench_base_price, strategy_start_date)
            elif not _idf.empty:
                # 回退：策略起始日前最近一个交易日
                _idf_sorted = _idf.sort_values('ds')
                before = _idf_sorted[_idf_sorted['ds'] <= strategy_start_date]
                if not before.empty:
                    _bench_base_price = float(before.iloc[-1][close_col])
                else:
                    _bench_base_price = float(_idf_sorted.iloc[0][close_col])
                logger.info("[Realtime] 基准基数(回退): %.2f", _bench_base_price)
    except Exception:
        pass

    last_flush = 0
    last_nav_date = ''

    # 启动时立即追加当日净值（如果 nav_series 还没有今天的记录）
    today_str = datetime.now().strftime('%Y%m%d')
    try:
        existing = _realtime_store._get_conn().execute(
            "SELECT COUNT(*) FROM nav_series WHERE date=?", (today_str,)
        ).fetchone()
        if not existing or existing[0] == 0:
            # 用当前 paper_state 计算
            pos_list = _paper_state.get('positions', [])
            total_mv = sum(p.get('market_value', 0) for p in pos_list)
            cash = _paper_state['account'].get('capital', 0)
            init_cap = _paper_state['account'].get('initial_capital', cash + total_mv)
            tv = cash + total_mv
            nav = tv / init_cap if init_cap > 0 else 1.0
            _realtime_store.append_nav({
                'date': today_str, 'nav': nav,
                'total_value': tv, 'cash': cash,
                'position_count': len(pos_list),
                'daily_return': (tv - init_cap) / init_cap if init_cap > 0 else 0,
            })
            _realtime_store.append_position_snapshot(today_str, {
                p['stockcode']: p for p in pos_list
            })
            _realtime_store.flush()
            # 同步更新 Web 面板
            pnl = _paper_state.get('pnl_curve', [])
            if not pnl or pnl[-1].get('date') != today_str:
                pnl.append({'date': today_str, 'nav': nav})
            _paper_state['pnl_curve'] = pnl
            logger.info("[Realtime] 启动追加当日净值: %s nav=%.4f", today_str, nav)
    except Exception as e:
        logger.warning("[Realtime] 启动净值追加失败: %s", e)

    while _realtime_running:
        try:
            if _realtime_targets:
                # 获取实时行情
                live_ticks = provider.get_ticks_batch(_realtime_targets)
                if live_ticks:
                    # 更新内存中的持仓价格
                    positions = _paper_state.get('positions', [])
                    for pos in positions:
                        code = pos.get('stockcode', '')
                        tick = live_ticks.get(code)
                        if tick and tick.last_price > 0:
                            pos['price'] = tick.last_price
                            qty = pos.get('quantity', 0)
                            pos['market_value'] = qty * tick.last_price
                            pos['unrealized_pnl'] = qty * (tick.last_price - pos.get('avg_cost', 0))
                            pos['return_rate'] = (tick.last_price - pos.get('avg_cost', 0)) / max(pos.get('avg_cost', 0.01), 0.01)

                    # 重算 total_value / total_return
                    total_mv = sum(p.get('market_value', 0) for p in positions)
                    cash = _paper_state['account'].get('capital', 0)
                    init_cap = _paper_state['account'].get('initial_capital',
                                 _paper_state['account'].get('capital', 0) + total_mv)
                    _paper_state['account']['total_value'] = cash + total_mv
                    if init_cap > 0:
                        _paper_state['account']['total_return'] = (cash + total_mv - init_cap) / init_cap

                    # 基准净值：盘中实时覆盖当日值
                    if _bench_base_price and _bench_base_price > 0:
                        try:
                            bench_tick = provider.get_tick('000852.SH')
                            if bench_tick and bench_tick.last_price > 0:
                                bm_nav = bench_tick.last_price / _bench_base_price
                                today_str2 = datetime.now().strftime('%Y%m%d')
                                bm_list = _paper_state.get('benchmark_nav', [])
                                # 覆盖/追加当日条目
                                if bm_list and bm_list[-1].get('date') == today_str2:
                                    bm_list[-1]['nav'] = bm_nav  # 覆盖
                                else:
                                    bm_list.append({'date': today_str2, 'nav': bm_nav})
                                _paper_state['benchmark_nav'] = bm_list[-60:]
                        except Exception:
                            pass

                    # 交易时段采集日内快照
                    now = datetime.now()
                    h, m = now.hour, now.minute
                    in_session = ((h == 9 and m >= 30) or h == 10 or
                                  (h == 11 and m <= 30) or (h >= 13 and h < 15))
                    if in_session:
                        intraday = _paper_state.get('intraday', [])
                        if not intraday:
                            _paper_state['intraday'] = []
                        _paper_state['intraday'].append({
                            'time': now.strftime('%H:%M:%S'),
                            'capital': cash,
                            'total_value': cash + total_mv,
                            'positions': len(positions),
                        })
                        # 只保留当天的快照，且不超过 240 个点
                        today = now.strftime('%Y-%m-%d')
                        _paper_state['intraday'] = [
                            s for s in _paper_state['intraday']
                            if s.get('time', '').startswith(today[:4]) or today in str(s.get('time', ''))
                        ][-240:]

                # 净值追加：启动时或收盘后写入
                now = datetime.now()
                today_str = now.strftime('%Y%m%d')
                if today_str != last_nav_date and (now.hour >= 15 or last_nav_date == ''):
                    try:
                        positions = _paper_state.get('positions', [])
                        total_mv = sum(p.get('market_value', 0) for p in positions)
                        cash = _paper_state['account'].get('capital', 0)
                        tv = cash + total_mv
                        init_cap = _paper_state['account'].get('initial_capital', tv)
                        nav = tv / init_cap if init_cap > 0 else 1.0

                        # 写 DB
                        _realtime_store.append_nav({
                            'date': today_str,
                            'nav': nav,
                            'total_value': tv,
                            'cash': cash,
                            'position_count': len(positions),
                            'daily_return': (tv - init_cap) / init_cap if init_cap > 0 else 0,
                        })
                        _realtime_store.append_position_snapshot(today_str, {
                            p['stockcode']: p for p in positions
                        })

                        # 同步更新 Web 面板的净值曲线
                        pnl = _paper_state.get('pnl_curve', [])
                        if not pnl or pnl[-1].get('date') != today_str:
                            pnl.append({'date': today_str, 'nav': nav})
                        _paper_state['pnl_curve'] = pnl

                        last_nav_date = today_str
                        logger.info("[Realtime] 净值已追加: %s nav=%.4f", today_str, nav)
                    except Exception as e:
                        logger.warning("[Realtime] 净值追加失败: %s", e)

                # 定期持久化
                now_ts = _time.time()
                if _realtime_store and (now_ts - last_flush) >= flush_interval:
                    try:
                        _realtime_store.save_positions({
                            p['stockcode']: p for p in _paper_state.get('positions', [])
                        })
                        _realtime_store.save_account(_paper_state['account'])
                        _realtime_store.flush()
                        last_flush = now_ts
                    except Exception as e:
                        logger.warning("[Realtime] flush 失败: %s", e)

        except Exception as e:
            logger.warning("[Realtime] 更新异常: %s", e)

        _time.sleep(interval)


def run_production(host: str = '0.0.0.0', port: int = 8899):
    """使用 waitress 生产级 WSGI 服务器启动。"""
    try:
        from waitress import serve
        print(f"[Server] waitress 启动 http://{host}:{port}")
        serve(app, host=host, port=port)
    except ImportError:
        print("[Server] waitress 未安装，回退到 Flask 开发服务器")
        print("[Server] 安装: pip install waitress")
        app.run(host=host, port=port, debug=False)


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(_PKG_DIR, 'templates'),
        static_folder=os.path.join(_PKG_DIR, 'static'),
    )
    _register_routes(app)
    return app


app = create_app()


if __name__ == '__main__':
    print(f"\n{'='*60}")
    print(f"  实盘模拟监控面板")
    print(f"  Paper Trading Dashboard")
    print(f"{'='*60}")
    print(f"  访问: http://127.0.0.1:8899")
    print(f"{'='*60}\n")
    app.run(debug=False, host='0.0.0.0', port=8899)
