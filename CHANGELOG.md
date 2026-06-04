# Changelog

## [0.2.0] - 2026-06-04

### Features
- 篮子交易: QMT 兼容 set_basket() + passorder(OP_BUY_BASKET) 多股同时下单
- 篮子三种下单模式: 按份数(2101) / 按金额(2102) / 按可用资金比例(2103)
- 算法交易: order_algo() 支持 TWAP/VWAP 大单拆分执行
- 数据提供者: WebSocketDataProvider (ws://) + SinaDataProvider (新浪财经 HTTP)
- 交易日历: is_trading_day 优先使用 akshare 交易日历，支持自定义日历

### Changed
- scheduler.py: is_trading_day 移除简化判断，改用 akshare 交易日历
- data_provider.py: 实现 WebSocketDataProvider + SinaDataProvider 双路径
- passorder: 新增篮子分发 (opType=35/36)，向后兼容单股下单

### Infrastructure
- 新增可选依赖: trading-calendar[akshare], ws[websocket-client], sina[requests]

## [0.1.0] - 2026-06-03

### Features
- PaperEngine: 实盘模拟引擎 (before_market → market_session → after_market)
- PaperBroker: 模拟券商 (T+1/涨跌停/佣金印花税/最小交易单位)
- QMT 兼容层: passorder / Context / init+handlebar 策略接口
- Web 监控面板: 净值曲线/持仓明细/成交记录/日内走势 (ECharts)
- 调度器: once / daily / daemon 三种运行模式
- 示例策略: 尾盘等权建仓 + 止损止盈
