# Changelog

## [0.1.0] - 2026-06-03

### Features
- PaperEngine: 实盘模拟引擎 (before_market → market_session → after_market)
- PaperBroker: 模拟券商 (T+1/涨跌停/佣金印花税/最小交易单位)
- QMT 兼容层: passorder / Context / init+handlebar 策略接口
- Web 监控面板: 净值曲线/持仓明细/成交记录/日内走势 (ECharts)
- 调度器: once / daily / daemon 三种运行模式
- 示例策略: 尾盘等权建仓 + 止损止盈
