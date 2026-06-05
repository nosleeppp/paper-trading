# Changelog

## [0.4.0] - 2026-06-04

### 新增 (Features)
- **IncrementalEngine**: 增量因子计算引擎，支持 gap 检测、部分因子更新、NaN 自动填充
- **OnlineEngine**: 在线因子计算引擎，为 quant_backtest 回测系统提供逐日实时因子计算
- **FactorStore**: 统一因子数据读写层，支持长表/宽表读取、追加写入、元数据查询
- **views.py**: 统一视图注册模块，消除 5 处重复的视图注册代码
- **BatchEngine**: 新增 `compute()` / `compute_long()` / `compute_single_date()` 内存返回模式
- FactorEngine 别名：`FactorEngine = BatchEngine`，保持向后兼容

### 重构 (Refactored)
- **config.py**: 单体 2929 行文件按类别拆分为 `factors/` 子包（估值/动量/流动性/波动性/资金流/财务/技术指标）
- **batch_engine.py**: 从 `core.py` 重构而来，集成 views.py
- **utils.py**: 保留向后兼容，推荐使用 `tools/` 子包

### 移除 (Removed)
- **flight_server.py**: 移除 Arrow Flight Server（DuckDB 本地直连已满足需求）
- **\_\_init\_\_.py**: 移除 FlightServer 导出

### 修复 (Fixed)
- Worker 子进程中硬编码路径 `/usr/local/lib/python3.12/dist-packages` → 动态路径
- Worker 日志 NullHandler 吞掉输出 → 标准 logging
- 因子计算异常静默丢弃 → logger.warning 记录
- `factor_processor.process_factor()` 中 fill_na 列名不匹配 bug
- `factor_processor.fill_na()` 中 Series name 未保留 bug

### 性能优化 (Performance)
- 统一视图注册减少 worker 初始化时间
- OnlineEngine 物化 prep 表到内存，避免重复磁盘扫描
- 单日期 compute() 优化：因子分类 → 分区裁剪 → Pandas merge

## [0.3.5] - 2026-05-22
### Features
- 财务表 ASOF 逻辑修复（LPAD 字符串排序键）
- v7.3 新增因子（HOLDER_NUM_CHANGE, OCF_TO_NETPROFIT 等 11 个）
- 进程池连接复用（run_process_pool）
- P2 优化：prep 缓存共享

## [0.2.25] - 2026-04-15
### Features
- 并行因子计算引擎（Plan B）
- prep 表缓存导出/加载
- 全市场股票池对齐

## [0.1.0] - 2026-03-01
### Base
- 初始版本发布
- DuckDB 因子计算引擎 v7.2
- 7 大类 110+ 因子配置
- 支持多进程并行
