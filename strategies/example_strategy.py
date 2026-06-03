"""
示例策略 — QMT 兼容写法
=======================
可直接迁移到讯投 QMT 实盘。

策略逻辑:
  - 每 14:50 检查持仓盈亏
  - 浮亏超 3% → 止损卖出
  - 浮盈超 5% → 止盈卖出
  - 空仓时按预设股票池等权买入
"""

# QMT 兼容: 这些函数由引擎注入
# passorder(opType, orderType, accountid, stockcode, quantity, price, ...)
# OP_BUY=23, OP_SELL=24, ORDER_MARKET=1102

# 策略参数
STOCK_POOL = ['600519.SH', '000858.SZ', '601318.SH', '600036.SH', '000333.SZ']
STOP_LOSS_PCT = -0.03
TAKE_PROFIT_PCT = 0.05
BUY_TIME = (14, 50)  # 尾盘买入
CHECK_TIME = (14, 50)


def init(C):
    """策略初始化 — QMT init(C) 兼容"""
    C.stock_pool = STOCK_POOL
    C.stop_loss_pct = STOP_LOSS_PCT
    C.take_profit_pct = TAKE_PROFIT_PCT
    C.has_bought_today = False
    print(f"[策略] 初始化完成, 股票池: {C.stock_pool}")


def handlebar(C):
    """主逻辑 — QMT handlebar(C) 兼容, 每K线触发"""
    current_h, current_m = C.current_time if hasattr(C, 'current_time') else (14, 50)
    current_t = (current_h, current_m)

    # 只在指定时间执行
    if current_t != CHECK_TIME:
        return

    # 检查现有持仓止损止盈
    positions = C.get_all_positions()
    for code in list(positions.keys()):
        pos = positions[code]
        if C.close and len(C.close) > 0:
            price = C.close[-1]
        else:
            tick = C.get_full_tick(code)
            price = tick.last_price if tick else 0
        if price <= 0 or pos.avg_cost <= 0:
            continue
        pnl_pct = (price - pos.avg_cost) / pos.avg_cost

        if pnl_pct <= C.stop_loss_pct:
            passorder(OP_SELL, ORDER_MARKET, C.accID, code,
                     pos.available, 0, C.strategy_name, ctx=C)
            print(f"[策略] 止损 {code} 盈亏={pnl_pct:.2%}")
        elif pnl_pct >= C.take_profit_pct:
            passorder(OP_SELL, ORDER_MARKET, C.accID, code,
                     pos.available, 0, C.strategy_name, ctx=C)
            print(f"[策略] 止盈 {code} 盈亏={pnl_pct:.2%}")

    # 空仓时等权买入
    if not C.has_bought_today and len(positions) == 0:
        available_cash = C.capital
        per_stock_cash = available_cash / len(C.stock_pool)
        for code in C.stock_pool:
            tick = C.get_full_tick(code)
            price = tick.last_price if tick else 0
            if price <= 0:
                continue
            qty = int(per_stock_cash / price / 100) * 100
            if qty >= 100:
                passorder(OP_BUY, ORDER_MARKET, C.accID, code,
                         qty, 0, C.strategy_name, ctx=C)
        C.has_bought_today = True
        print(f"[策略] 建仓完成")
