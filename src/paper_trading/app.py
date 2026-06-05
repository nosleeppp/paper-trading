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


def update_paper_state(report: dict):
    """由 engine 调用，写入实盘状态。"""
    _paper_state["account"] = {
        "capital": report.get("cash", 0),
        "total_value": report.get("total_value", 0),
        "total_return": report.get("total_return", 0),
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
         "price": t.get("price", 0)}
        for t in report.get("trades", [])
    ]
    _paper_state["pnl_curve"] = [
        {"date": s.get("time", ""),
         "nav": s.get("total_value", 0) / max(report.get("initial_capital", 1), 1)}
        for s in report.get("minute_snapshots", [])
    ]
    _paper_state["intraday"] = report.get("minute_snapshots", [])
    _paper_state["last_update"] = datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════════════
# 异步回测 runner
# ═══════════════════════════════════════════════════════════════════════

def _run_backtest_async(task_id: str, start_date: str, end_date: str,
                        strategy_module: str = None, capital: float = 100_000_000,
                        pythonpath: str = None, data_dir: str = None):
    """后台线程：运行 quant_backtest 回测。"""
    try:
        _backtest_tasks[task_id]["status"] = "running"

        # 将 pythonpath 加入搜索路径（使 quant_backtest 可被找到）
        if pythonpath:
            import sys
            for p in pythonpath.split(":"):
                p = p.strip()
                if p and p not in sys.path:
                    sys.path.insert(0, p)

        from quant_backtest import Backtester, DataCache

        data_dir = data_dir or '/root/lqq_bot_workspace/data'
        output_dir = os.path.join(os.path.dirname(data_dir), 'zz1000', 'output')

        data_cache = DataCache(data_dir=data_dir, duckdb_file='data.duckdb')

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

        if strategy_module:
            import importlib
            # strategy_module 可以是文件路径或模块名
            if strategy_module.endswith('.py') and os.path.exists(strategy_module):
                mod = importlib.import_module(
                    os.path.splitext(os.path.basename(strategy_module))[0]
                ) if '.' not in os.path.basename(strategy_module) else None
                if mod is None:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location('_bt_strategy', strategy_module)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
            else:
                mod = importlib.import_module(strategy_module)

            for name in dir(mod):
                obj = getattr(mod, name)
                if (isinstance(obj, type) and hasattr(obj, 'initialize')
                        and hasattr(obj, 'schedule_handler') and hasattr(obj, 'NAME')):
                    strategy = obj(data_dir=data_dir)
                    bt.set_strategies(
                        initialize=strategy.initialize,
                        schedule_handler=strategy.schedule_handler,
                    )
                    break

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
