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


def _register_routes(app):
    @app.route('/')
    def index():
        return render_template('index.html')

    # ── 实盘指标（用 quant_backtest 方法计算）─────────

    @app.route('/api/paper/metrics')
    def api_paper_metrics():
        """
        从 DB nav_series + index_daily.parquet 读取数据，
        调用 quant_backtest.calculate_performance_metrics 计算完整指标。
        """
        try:
            import pandas as pd

            # 1. 从 store 读 nav_series → daily_df
            if not _realtime_store:
                return jsonify({})
            nav_rows = _realtime_store._get_conn().execute(
                "SELECT date, nav, total_value, cash, position_count, daily_return "
                "FROM nav_series ORDER BY date"
            ).fetchall()
            if not nav_rows:
                return jsonify({})

            daily_df = pd.DataFrame(nav_rows, columns=[
                'date', 'nav', 'total_value', 'cash', 'position_count', 'daily_return'
            ])

            # 2. 从 store 读 orders → trades_df
            order_rows = _realtime_store._get_conn().execute(
                "SELECT trade_date, trade_time, stockcode, side, quantity, price, amount "
                "FROM orders ORDER BY id"
            ).fetchall()
            trades_df = None
            if order_rows:
                trades_df = pd.DataFrame(order_rows, columns=[
                    'trade_date', 'trade_time', 'stockcode', 'side', 'quantity', 'price', 'amount'
                ])

            # 3. 从 index_daily.parquet 读基准
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
                    benchmark_data.columns = ['date', 'close']
                    benchmark_data['date'] = benchmark_data['date'].astype(str).str[:8]

            # 4. 调用 quant_backtest 计算指标
            from quant_backtest import calculate_performance_metrics
            metrics = calculate_performance_metrics(daily_df, trades_df, benchmark_data)
            return jsonify(metrics)

        except ImportError:
            return jsonify({'error': 'quant_backtest 未安装，无法计算指标'})
        except Exception as e:
            return jsonify({'error': str(e)})

    # ── 实盘状态 ──────────────────────────────────────

    @app.route('/api/status')
    def api_status():
        return jsonify({
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime('%H:%M:%S'),
            **{k: v for k, v in _paper_state.items() if k != "last_update"},
        })

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
    # 基准净值：从 index_daily 读初始价格，后续用 Sina 实时价格计算
    _bench_base_price = None
    _bench_nav_series = []  # 基准净值序列（内存）
    try:
        idx_path = os.environ.get('PAPER_INDEX_DAILY_PATH', '')
        if idx_path and os.path.exists(idx_path):
            import pandas as _pd
            _idf = _pd.read_parquet(idx_path)
            code_col = next((c for c in ['ts_code','code'] if c in _idf.columns), _idf.columns[1])
            _idf = _idf[_idf[code_col].astype(str).str.replace('.SH','').str.replace('.SZ','') == '000852']
            if not _idf.empty:
                close_col = next((c for c in ['close','qfq_close'] if c in _idf.columns), _idf.columns[-1])
                _bench_base_price = float(_idf.iloc[-1][close_col])  # 最新收盘价作为基准
                logger.info("[Realtime] 基准初始价格: %.2f", _bench_base_price)
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

                    # 重算 total_value / total_return
                    total_mv = sum(p.get('market_value', 0) for p in positions)
                    cash = _paper_state['account'].get('capital', 0)
                    init_cap = _paper_state['account'].get('initial_capital',
                                 _paper_state['account'].get('capital', 0) + total_mv)
                    _paper_state['account']['total_value'] = cash + total_mv
                    if init_cap > 0:
                        _paper_state['account']['total_return'] = (cash + total_mv - init_cap) / init_cap

                    # 基准净值：取 000852.SH 实时价计算
                    if _bench_base_price and _bench_base_price > 0:
                        try:
                            bench_tick = provider.get_tick('000852.SH')
                            if bench_tick and bench_tick.last_price > 0:
                                bm_nav = bench_tick.last_price / _bench_base_price
                                today_str2 = datetime.now().strftime('%Y%m%d')
                                bm_list = _paper_state.get('benchmark_nav', [])
                                if not bm_list or bm_list[-1].get('date') != today_str2:
                                    bm_list.append({'date': today_str2, 'nav': bm_nav})
                                _paper_state['benchmark_nav'] = bm_list[-60:]  # 保留最近 60 点
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
