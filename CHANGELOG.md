# Changelog

## [0.12.0] - 2026-06-09

### Fixed
- pnl_curve 数据源从 backtest xlsx 改为 DB nav_series（包含 backtest 历史 + realtime 实时更新）
- 基准计算链彻底重写：归一化基数对齐策略起始日、benchmark_data 过滤到策略日期范围
- 重启时不覆盖 realtime 数据：nav_series 只插入不覆盖、从 store 恢复 _paper_state
- 日期格式全链路统一 YYYYMMDD
- 所有路径可配置：index_daily/daily_qfq/duckdb/strategy 均通过 config.json 或环境变量

### Added
- SQLite 持久化：PaperStore (account/positions/orders/nav_series/signals 5 表)
- 实时更新循环：每 60s 拉行情，每 5min flush DB，每日 15:00 后追加净值
- 复刻 quant_backtest 绩效指标计算：_calc_performance_metrics / _calc_benchmark_metrics / _calc_trade_stats
- systemd 部署：paper-trading.service + waitress 生产服务器
- 回测分析 Tab：本地文件夹加载 + 年度下拉框

## [0.4.0] - 2026-06-05

### Changed
- 架构重构: 分离通用部署模板与策略专属部署
- deploy/template/: 通用部署模板，任何策略可复制使用
- ICIR策略适配器从包内移除，独立到策略部署目录
- engine.py: run() 自动调用 update_paper_state() 同步 Web 面板
- 策略适配器: 路径全部改为环境变量驱动 (PAPER_DATA_DIR 等)

## [0.3.0] - 2026-06-05

### Features
- Web 可视化面板重构：实盘监控 + 回测分析双 Tab 布局
- 在线回测：输入日期区间，后台异步运行 quant_backtest，前端实时展示
- 导入回测：支持粘贴 JSON 或上传文件，即时渲染净值曲线/成交记录
- 实盘 vs 回测对比图：底部全宽双线叠加 ECharts 净值对比
- API: `/api/backtest/run`, `/api/backtest/result`, `/api/backtest/upload`, `/api/backtest/list`
- API: `/api/paper/update` 供 engine 写入实盘状态，`/api/status` 返回真实数据
- app.py 全局共享状态 `_paper_state` / `_backtest_tasks`
- 策略适配器: `strategies/pcat_icir_100_monthly.py` 桥接 quant_backtest → paper_trading

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
