"""
实盘模拟系统 - Web 监控面板 (Flask)
===================================
实时查看账户状态、持仓、成交记录、净值曲线。
支持回测结果展示：在线运行 / 导入已有结果。

启动:
    paper-trading web
    → http://127.0.0.1:8899
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import Flask, render_template, jsonify, request

_PKG_DIR = os.path.dirname(__file__)

# ═══════════════════════════════════════════════════════════════════════
# 全局共享状态
# ═══════════════════════════════════════════════════════════════════════

# 实盘状态：由 engine 写入
_paper_state: Dict[str, Any] = {
    "account": {
        "capital": 0,
        "total_value": 0,
        "total_return": 0,
        "position_count": 0,
    },
    "positions": [],
    "orders": [],
    "pnl_curve": [],
    "intraday": [],
    "last_update": None,
}

# 回测状态：{task_id: {status, result, error}}
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
        {
            "time": t.get("time", ""),
            "stockcode": t.get("stockcode", ""),
            "side": t.get("side", "BUY"),
            "quantity": t.get("quantity", 0),
            "price": t.get("price", 0),
        }
        for t in report.get("trades", [])
    ]
    _paper_state["pnl_curve"] = [
        {"date": s.get("time", ""), "nav": s.get("total_value", 0) / max(report.get("initial_capital", 1), 1)}
        for s in report.get("minute_snapshots", [])
    ]
    _paper_state["intraday"] = report.get("minute_snapshots", [])
    _paper_state["last_update"] = datetime.now().isoformat()


# ═══════════════════════════════════════════════════════════════════════
# 异步回测 runner
# ═══════════════════════════════════════════════════════════════════════

def _run_backtest_async(task_id: str, start_date: str, end_date: str,
                        strategy_module: str = None, capital: float = 100_000_000):
    """后台线程：运行 quant_backtest 回测。"""
    try:
        _backtest_tasks[task_id]["status"] = "running"

        from quant_backtest import Backtester, DataCache

        # 默认数据目录
        data_dir = '/root/lqq_bot_workspace/data'
        output_dir = '/root/lqq_bot_workspace/zz1000/output'

        data_cache = DataCache(data_dir=data_dir, duckdb_file='data.duckdb')

        bt = Backtester(
            start_date=start_date,
            end_date=end_date,
            initial_capital=capital,
            benchmark='000852.SH',
            commission_rate=0.0001,
            min_commission=5.0,
            slippage=0.001,
            stamp_duty_rate=0.001,
            price_type='qfq',
            data_dir=data_dir,
            strategy_name=f'backtest_{task_id}',
            output_dir=output_dir,
            output_charts=False,
            data_cache=data_cache,
        )

        # 如果指定了策略模块，动态加载
        if strategy_module:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                '_bt_strategy', strategy_module
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # 查找策略类
            for name in dir(mod):
                obj = getattr(mod, name)
                if (isinstance(obj, type) and
                    hasattr(obj, 'initialize') and
                    hasattr(obj, 'schedule_handler') and
                    hasattr(obj, 'NAME')):
                    strategy = obj(data_dir=data_dir)
                    bt.set_strategies(
                        initialize=strategy.initialize,
                        schedule_handler=strategy.schedule_handler,
                    )
                    break

        bt.run()

        # 提取结果
        stats = getattr(bt, 'stats', {}) or {}
        nav_df = getattr(bt, 'nav_df', None)
        trades_df = getattr(bt, 'trades_df', None)

        result = {
            "account": {
                "capital": stats.get("final_cash", capital),
                "total_value": stats.get("final_value", capital),
                "total_return": stats.get("total_return", 0),
                "position_count": stats.get("position_count", 0),
                "sharpe": stats.get("sharpe_ratio", 0),
                "max_drawdown": stats.get("max_drawdown", 0),
                "annual_return": stats.get("annual_return", 0),
                "win_rate": stats.get("win_rate", 0),
            },
            "nav_series": [],
            "trades": [],
        }

        if nav_df is not None:
            import pandas as pd
            nav_records = nav_df.to_dict(orient='records') if isinstance(nav_df, pd.DataFrame) else []
            result["nav_series"] = [
                {"date": str(r.get("date", "")), "nav": float(r.get("nav", r.get("total_value", 0)))}
                for r in nav_records
            ]

        if trades_df is not None:
            import pandas as pd
            trade_records = trades_df.to_dict(orient='records') if isinstance(trades_df, pd.DataFrame) else []
            result["trades"] = [
                {
                    "time": str(r.get("date", r.get("trade_date", ""))),
                    "stockcode": str(r.get("ts_code", "")),
                    "side": "BUY" if str(r.get("side", "")).upper() in ("BUY", "买入", "B") else "SELL",
                    "quantity": int(r.get("quantity", r.get("vol", 0))),
                    "price": float(r.get("price", 0)),
                }
                for r in trade_records
            ]

        _backtest_tasks[task_id]["status"] = "done"
        _backtest_tasks[task_id]["result"] = result

    except ImportError as e:
        _backtest_tasks[task_id]["status"] = "error"
        _backtest_tasks[task_id]["error"] = (
            f"quant_backtest 未安装或不可用: {e}\n"
            "请使用「导入结果」模式，或将策略文件放在服务器上运行。"
        )
    except Exception:
        _backtest_tasks[task_id]["status"] = "error"
        _backtest_tasks[task_id]["error"] = traceback.format_exc()


# ═══════════════════════════════════════════════════════════════════════
# Flask app
# ═══════════════════════════════════════════════════════════════════════

def _register_routes(app):
    # ── 页面 ──────────────────────────────────────────

    @app.route('/')
    def index():
        return render_template('index.html')

    # ── 实盘状态 ──────────────────────────────────────

    @app.route('/api/status')
    def api_status():
        """返回实盘模拟当前状态。"""
        return jsonify({
            "date": datetime.now().strftime('%Y-%m-%d'),
            "time": datetime.now().strftime('%H:%M:%S'),
            **{k: v for k, v in _paper_state.items() if k != "last_update"},
        })

    @app.route('/api/paper/update', methods=['POST'])
    def api_paper_update():
        """由 engine 调用，更新实盘状态。"""
        data = request.get_json(force=True)
        update_paper_state(data)
        return jsonify({"success": True})

    # ── 在线回测 ──────────────────────────────────────

    @app.route('/api/backtest/run', methods=['POST'])
    def api_backtest_run():
        """启动异步回测任务。"""
        global _backtest_task_counter
        data = request.get_json(force=True) or {}

        start_date = data.get('start_date', '20240101')
        end_date = data.get('end_date', datetime.now().strftime('%Y%m%d'))
        strategy_module = data.get('strategy_module', None)
        capital = float(data.get('capital', 100_000_000))

        _backtest_task_counter += 1
        task_id = f"bt_{_backtest_task_counter}"

        _backtest_tasks[task_id] = {"status": "pending", "result": None, "error": None}

        thread = threading.Thread(
            target=_run_backtest_async,
            args=(task_id, start_date, end_date, strategy_module, capital),
            daemon=True,
        )
        thread.start()

        return jsonify({"task_id": task_id})

    @app.route('/api/backtest/result/<task_id>')
    def api_backtest_result(task_id):
        """获取回测任务结果。"""
        task = _backtest_tasks.get(task_id)
        if task is None:
            return jsonify({"status": "not_found", "error": "未知任务 ID"}), 404
        return jsonify({
            "status": task["status"],
            "result": task.get("result"),
            "error": task.get("error"),
        })

    # ── 导入回测结果 ─────────────────────────────────

    @app.route('/api/backtest/upload', methods=['POST'])
    def api_backtest_upload():
        """
        导入已有回测结果。
        """
        data = request.get_json(force=True) or {}

        if not data:
            return jsonify({"success": False, "error": "请求体为空"}), 400

        global _backtest_task_counter
        _backtest_task_counter += 1
        task_id = f"upload_{_backtest_task_counter}"

        _backtest_tasks[task_id] = {
            "status": "done",
            "result": {
                "account": data.get("account", {}),
                "nav_series": data.get("nav_series", []),
                "trades": data.get("trades", []),
                "positions": data.get("positions", []),
            },
            "error": None,
        }

        return jsonify({"success": True, "task_id": task_id})

    @app.route('/api/backtest/list')
    def api_backtest_list():
        """列出所有回测任务。"""
        tasks = []
        for tid, t in _backtest_tasks.items():
            tasks.append({
                "task_id": tid,
                "status": t["status"],
                "has_result": t.get("result") is not None,
                "error": t.get("error"),
            })
        return jsonify({"tasks": tasks})


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
