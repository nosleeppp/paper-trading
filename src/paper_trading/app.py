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

        data_dir = data_dir or '/root/lqq_bot_workspace/data'
        output_dir = os.path.join(os.path.dirname(data_dir), 'zz1000', 'output')

        data_cache = DataCache(data_dir=data_dir, duckdb_file='data.duckdb')

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
    """解析 收益概述.txt → account dict。"""
    text = content.decode('utf-8')
    result = {}

    def _pct(s: str) -> float:
        try:
            return float(s.replace('%', '')) / 100
        except ValueError:
            return 0.0

    def _num(s: str) -> float:
        try:
            return float(s)
        except ValueError:
            return 0.0

    patterns = {
        'total_return': (r'总收益率:\s*([\d\-.]+%)', _pct),
        'annual_return': (r'年化收益率:\s*([\d\-.]+%)', _pct),
        'benchmark_return': (r'基准收益率:\s*([\d\-.]+%)', _pct),
        'excess_return': (r'超额收益率:\s*([\d\-.]+%)', _pct),
        'alpha': (r'Alpha:\s*([\d\-.]+)', _num),
        'beta': (r'Beta:\s*([\d\-.]+)', _num),
        'sharpe': (r'夏普比率:\s*([\d\-.]+)', _num),
        'max_drawdown': (r'最大回撤:\s*([\d\-.]+%)', _pct),
        'volatility': (r'年化波动率:\s*([\d\-.]+%)', _pct),
        'tracking_error': (r'跟踪误差:\s*([\d\-.]+%)', _pct),
        'information_ratio': (r'信息比率:\s*([\d\-.]+)', _num),
        'win_rate': (r'胜率:\s*([\d\-.]+%)', _pct),
        'profit_loss_ratio': (r'盈亏比:\s*([\d\-.]+)', _num),
        'trade_count': (r'交易次数:\s*(\d+)', int),
        'start_date': (r'回测区间:\s*(\d{8})\s*~', str),
        'end_date': (r'~\s*(\d{8})', str),
    }

    for key, (pattern, converter) in patterns.items():
        m = re.search(pattern, text)
        if m:
            result[key] = converter(m.group(1))

    return result


# ═══════════════════════════════════════════════════════════════════════
# Flask app
# ═══════════════════════════════════════════════════════════════════════

def _register_routes(app):
    @app.route('/')
    def index():
        return render_template('index.html')

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

    # ── 在线回测 ──────────────────────────────────────

    @app.route('/api/backtest/run', methods=['POST'])
    def api_backtest_run():
        global _backtest_task_counter
        data = request.get_json(force=True) or {}

        start_date = data.get('start_date', '20240101')
        end_date = data.get('end_date', datetime.now().strftime('%Y%m%d'))
        strategy_module = data.get('strategy_module', None)
        capital = float(data.get('capital', 100_000_000))
        pythonpath = data.get('pythonpath', None)
        data_dir = data.get('data_dir', None)

        _backtest_task_counter += 1
        task_id = f"bt_{_backtest_task_counter}"
        _backtest_tasks[task_id] = {"status": "pending", "result": None, "error": None}

        threading.Thread(
            target=_run_backtest_async,
            args=(task_id, start_date, end_date, strategy_module, capital,
                  pythonpath, data_dir),
            daemon=True,
        ).start()

        return jsonify({"task_id": task_id})

    @app.route('/api/backtest/result/<task_id>')
    def api_backtest_result(task_id):
        task = _backtest_tasks.get(task_id)
        if task is None:
            return jsonify({"status": "not_found"}), 404
        return jsonify({
            "status": task["status"],
            "result": task.get("result"),
            "error": task.get("error"),
        })

    # ── 导入回测结果（文件上传）─────────────────────────

    @app.route('/api/backtest/upload', methods=['POST'])
    def api_backtest_upload():
        """
        上传 quant_backtest 输出文件，自动解析。
        文件字段名（均选填，至少传一个）:
          trades    → 交易记录.csv
          positions → 持仓记录.csv
          nav       → 净值序列.xlsx
          summary   → 收益概述.txt
        """
        global _backtest_task_counter

        account = {}
        nav_series = []
        trades = []
        positions = []

        # 解析上传的每个文件
        if 'trades' in request.files:
            try:
                trades = _parse_trades_csv(request.files['trades'].read())
            except Exception as e:
                return jsonify({"success": False, "error": f"交易记录解析失败: {e}"}), 400

        if 'positions' in request.files:
            try:
                positions = _parse_positions_csv(request.files['positions'].read())
            except Exception as e:
                return jsonify({"success": False, "error": f"持仓记录解析失败: {e}"}), 400

        if 'nav' in request.files:
            try:
                nav_series = _parse_nav_xlsx(request.files['nav'].read())
            except Exception as e:
                return jsonify({"success": False, "error": f"净值序列解析失败: {e}"}), 400

        if 'summary' in request.files:
            try:
                account = _parse_summary_txt(request.files['summary'].read())
            except Exception as e:
                return jsonify({"success": False, "error": f"收益概述解析失败: {e}"}), 400

        # 如果没有上传文件，尝试 JSON
        if not any([trades, positions, nav_series, account]):
            data = request.get_json(silent=True) or {}
            if data:
                account = data.get("account", {})
                nav_series = data.get("nav_series", [])
                trades = data.get("trades", [])
                positions = data.get("positions", [])
            else:
                return jsonify({"success": False, "error": "请上传至少一个文件"}), 400

        _backtest_task_counter += 1
        task_id = f"upload_{_backtest_task_counter}"

        _backtest_tasks[task_id] = {
            "status": "done",
            "result": {
                "account": account,
                "nav_series": nav_series,
                "trades": trades,
                "positions": positions,
                "drawdown_series": _calc_drawdown(nav_series),
            },
            "error": None,
        }

        return jsonify({"success": True, "task_id": task_id,
                        "parsed": {"nav_days": len(nav_series), "trades": len(trades),
                                   "positions": len(positions), "account_keys": list(account.keys())}})

    @app.route('/api/strategies')
    def api_strategies():
        """扫描文件系统返回可用策略文件列表。只返回包含策略类定义的 .py 文件。"""
        search_roots = [
            os.environ.get('PAPER_COLLAB_ROOT', '/root/lqq_bot_workspace'),
            '/root/lqq_bot_workspace',
        ]
        # 策略类特征：import FactorStrategyTemplate 或 class NAME 或 _load_factor_chunk
        STRATEGY_MARKERS = ('FactorStrategyTemplate', '_load_factor_chunk',
                            'def _on_select', 'def schedule_handler')
        strategies = []
        seen = set()
        for root in search_roots:
            if not os.path.isdir(root):
                continue
            for dirpath, dirs, files in os.walk(root):
                depth = dirpath.count(os.sep) - root.count(os.sep)
                if depth > 4:
                    dirs.clear(); continue
                for f in sorted(files):
                    if not f.endswith('.py') or f.startswith('_') or f.startswith('test'):
                        continue
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, root)
                    if rel in seen:
                        continue
                    # 内容验证：快速扫描文件确认包含策略类定义
                    try:
                        with open(full, 'r', encoding='utf-8', errors='ignore') as fh:
                            head = fh.read(4096)  # 只读前 4KB
                        if not any(m in head for m in STRATEGY_MARKERS):
                            continue
                    except Exception:
                        continue
                    seen.add(rel)
                    strategies.append({
                        'name': f.replace('.py', ''),
                        'path': full,
                        'relpath': rel,
                    })
        return jsonify(strategies)

    @app.route('/api/trade_dates')
    def api_trade_dates():
        """返回有成交记录的日期列表（供前端下拉框）。"""
        if _realtime_store:
            rows = _realtime_store._get_conn().execute(
                "SELECT DISTINCT trade_date FROM orders ORDER BY trade_date DESC"
            ).fetchall()
            return jsonify([r[0] for r in rows])
        return jsonify([])

    @app.route('/api/benchmark')
    def api_benchmark():
        """
        基准净值（000852.SH 中证1000），从 index_daily.parquet 直接读取。
        归一化：以策略首日为 1.0。
        不依赖 _realtime_store，纯文件读取。
        """
        try:
            import pandas as pd
            data_dir = os.environ.get('PAPER_DATA_DIR', '/root/lqq_bot_workspace/data')
            idx_path = os.path.join(data_dir, 'index_daily.parquet')
            if not os.path.exists(idx_path):
                return jsonify({'error': f'index_daily.parquet 不存在: {idx_path}'}), 404

            df = pd.read_parquet(idx_path)
            # 列名可能是 trade_date/ts_code/close 或 date/code/close
            date_col = next((c for c in ['trade_date', 'date'] if c in df.columns), df.columns[0])
            code_col = next((c for c in ['ts_code', 'code'] if c in df.columns), df.columns[1])
            close_col = next((c for c in ['close', 'qfq_close'] if c in df.columns), df.columns[-1])

            bench = df[df[code_col].astype(str).str.replace('.SH', '').str.replace('.SZ', '') == '000852']
            if bench.empty:
                return jsonify({'error': 'index_daily.parquet 中未找到 000852'}), 404

            bench = bench.sort_values(date_col)
            bench['date_str'] = bench[date_col].astype(str).str[:8]
            base_price = float(bench.iloc[0][close_col])

            if base_price <= 0:
                return jsonify({'error': '基准首日价格为0'}), 500

            result = []
            for _, row in bench.iterrows():
                result.append({
                    'date': str(row[date_col])[:10].replace('-', ''),
                    'nav': float(row[close_col]) / base_price,
                })
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
    last_flush = 0
    last_nav_date = ''  # 防止同一天重复写 nav

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

                # 每日收盘后追加净值（15:00 后写一次）
                now = datetime.now()
                today_str = now.strftime('%Y%m%d')
                if now.hour >= 15 and today_str != last_nav_date:
                    try:
                        positions = _paper_state.get('positions', [])
                        total_mv = sum(p.get('market_value', 0) for p in positions)
                        cash = _paper_state['account'].get('capital', 0)
                        tv = cash + total_mv
                        init_cap = _paper_state['account'].get('initial_capital', tv)
                        nav = tv / init_cap if init_cap > 0 else 1.0

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
