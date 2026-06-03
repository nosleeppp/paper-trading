"""
实盘模拟系统 - Web 监控面板 (Flask)
===================================
实时查看账户状态、持仓、成交记录、净值曲线。

启动:
    paper-trading web
    → http://127.0.0.1:8899
"""

import json
import os
from datetime import datetime

from flask import Flask, render_template, jsonify, request

_PKG_DIR = os.path.dirname(__file__)


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(_PKG_DIR, 'templates'),
        static_folder=os.path.join(_PKG_DIR, 'static'),
    )
    _register_routes(app)
    return app


app = create_app()


def _register_routes(app):
    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/api/status')
    def api_status():
        """返回当前账户状态（占位——由各组件注入实时数据）"""
        return jsonify({
            'date': datetime.now().strftime('%Y-%m-%d'),
            'time': datetime.now().strftime('%H:%M:%S'),
            'account': {
                'capital': 0,
                'total_value': 0,
                'total_return': 0,
                'position_count': 0,
            },
            'positions': [],
            'orders': [],
            'pnl_curve': [],
        })


if __name__ == '__main__':
    print(f"\n{'='*60}")
    print(f"  实盘模拟监控面板")
    print(f"  Paper Trading Dashboard")
    print(f"{'='*60}")
    print(f"  访问: http://127.0.0.1:8899")
    print(f"{'='*60}\n")
    app.run(debug=False, host='0.0.0.0', port=8899)
