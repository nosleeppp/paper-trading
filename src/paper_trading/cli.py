"""
CLI 入口 — paper-trading 命令
===============================
paper-trading run      → 单次运行
paper-trading web      → 启动 Web 面板
paper-trading daemon   → 守护进程
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description='实盘模拟系统')
    sub = parser.add_subparsers(dest='command')

    p_run = sub.add_parser('run', help='单次运行')
    p_run.add_argument('--strategy', '-s', required=True, help='策略文件路径')
    p_run.add_argument('--capital', '-c', type=float, default=1_000_000, help='初始资金')
    p_run.add_argument('--date', '-d', default=None, help='运行日期 YYYYMMDD')

    p_web = sub.add_parser('web', help='启动监控面板')
    p_web.add_argument('--port', '-p', type=int, default=8899)

    p_daemon = sub.add_parser('daemon', help='守护进程')
    p_daemon.add_argument('--strategy', '-s', required=True)
    p_daemon.add_argument('--capital', '-c', type=float, default=1_000_000)

    args = parser.parse_args()

    if args.command == 'run':
        from paper_trading.engine import PaperEngine
        from paper_trading.data_provider import DataProvider
        engine = PaperEngine(
            strategy_file=args.strategy,
            initial_capital=args.capital,
            data_provider=DataProvider(),
        )
        report = engine.run(trade_date=args.date)
        engine.print_summary(report)

    elif args.command == 'web':
        from paper_trading.app import app
        print(f"\n实盘模拟监控面板: http://127.0.0.1:{args.port}\n")
        app.run(debug=False, host='0.0.0.0', port=args.port)

    elif args.command == 'daemon':
        from paper_trading.scheduler import run_daemon
        run_daemon(strategy_file=args.strategy, initial_capital=args.capital)

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
