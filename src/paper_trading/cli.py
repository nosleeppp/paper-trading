"""
CLI 入口 — paper-trading 命令
===============================
paper-trading run      → 单次运行
paper-trading web      → 启动 Web 面板
paper-trading daemon   → 守护进程
"""

import argparse
import sys


def _load_trade_calendar(calendar_path: str):
    """从文件加载交易日历，返回 date 集合。"""
    if not calendar_path:
        return None
    from datetime import date
    dates = set()
    with open(calendar_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            clean = line.replace('-', '')
            if len(clean) == 8 and clean.isdigit():
                dates.add(date(int(clean[:4]), int(clean[4:6]), int(clean[6:8])))
    print(f"[CLI] 交易日历已加载: {len(dates)} 天 (来源: {calendar_path})")
    return dates


def main():
    parser = argparse.ArgumentParser(description='实盘模拟系统')
    sub = parser.add_subparsers(dest='command')

    p_run = sub.add_parser('run', help='单次运行')
    p_run.add_argument('--strategy', '-s', required=True, help='策略文件路径')
    p_run.add_argument('--capital', '-c', type=float, default=1_000_000, help='初始资金')
    p_run.add_argument('--date', '-d', default=None, help='运行日期 YYYYMMDD')
    p_run.add_argument('--trade-calendar', '-t', default=None, help='交易日历文件路径')

    p_web = sub.add_parser('web', help='启动监控面板')
    p_web.add_argument('--port', '-p', type=int, default=8899)
    p_web.add_argument('--trade-calendar', '-t', default=None, help='交易日历文件路径')

    p_daemon = sub.add_parser('daemon', help='守护进程')
    p_daemon.add_argument('--strategy', '-s', required=True)
    p_daemon.add_argument('--capital', '-c', type=float, default=1_000_000)
    p_daemon.add_argument('--trade-calendar', '-t', default=None, help='交易日历文件路径')

    args = parser.parse_args()

    trade_calendar = None
    if hasattr(args, 'trade_calendar') and args.trade_calendar:
        trade_calendar = _load_trade_calendar(args.trade_calendar)

    if args.command == 'run':
        from paper_trading.scheduler import set_trade_calendar_file
        if args.trade_calendar:
            set_trade_calendar_file(args.trade_calendar)
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
        from paper_trading.scheduler import set_trade_calendar_file
        if args.trade_calendar:
            set_trade_calendar_file(args.trade_calendar)
        from paper_trading.app import app
        print(f"\n实盘模拟监控面板: http://127.0.0.1:{args.port}\n")
        app.run(debug=False, host='0.0.0.0', port=args.port)

    elif args.command == 'daemon':
        from paper_trading.scheduler import run_daemon, set_trade_calendar_file
        if args.trade_calendar:
            set_trade_calendar_file(args.trade_calendar)
        run_daemon(
            strategy_file=args.strategy,
            initial_capital=args.capital,
            trade_calendar=trade_calendar,
        )

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
