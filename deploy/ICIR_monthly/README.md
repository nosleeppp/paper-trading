# ICIR_monthly 实盘模拟 — 部署与运行指南

## 目录结构

```
/root/lqq_bot_workspace/ICIR_monthly/
├── config.json              ← ① 编辑配置文件
├── setup.sh                 ← ② 运行安装
├── README.md                ← 本文件
├── venv/                    ← (自动创建) Python 虚拟环境
├── scripts/
│   └── bootstrap.py         ← ③ 启动脚本
├── strategies/
│   └── pcat_icir_100_monthly.py  ← 策略适配器
└── signals/
    └── paper_signal_targets.json  ← (自动生成) 信号文件
```

## 一、前置条件

服务器上需要有以下内容（路径由 config.json 指定）：

| 项目 | 说明 |
|------|------|
| DuckDB 因子库 | `data_dir/data.duckdb`，包含 `factors_2017` ~ `factors_2026` 年度表 |
| 回测策略模块 | `collab_root/IC5_T100_ICIRTM10_ICIRN20_月频_218因子.py` |
| quant_backtest | `pip install quant_backtest>=0.7.18`（如果使用在线信号生成） |

## 二、部署步骤

### Step 1：创建目录

```bash
mkdir -p /root/lqq_bot_workspace/ICIR_monthly
cd /root/lqq_bot_workspace/ICIR_monthly
```

### Step 2：上传文件

将以下文件从本仓库 `deploy/ICIR_monthly/` 上传到服务器：

```bash
# 在本地执行
scp -r deploy/ICIR_monthly/* root@<server>:/root/lqq_bot_workspace/ICIR_monthly/
```

上传后的文件：
- `config.json`
- `setup.sh`
- `scripts/bootstrap.py`
- `strategies/pcat_icir_100_monthly.py`
- `README.md`（本文件）

### Step 3：编辑配置

```bash
vim config.json
```

必须修改的字段：

```json
{
    "data_dir": "/root/lqq_bot_workspace/data",
    "output_dir": "/root/lqq_bot_workspace/zz1000/output",
    "collab_root": "/root/lqq_bot_workspace",
    "strategy": {
        "module": "IC5_T100_ICIRTM10_ICIRN20_月频_218因子"
    },
    "capital": 100000000,
    "start_date": "20250529"
}
```

### Step 4：运行安装

```bash
chmod +x setup.sh
./setup.sh
```

这会：
1. 创建 Python 虚拟环境 `venv/`
2. 安装 `paper_trading>=0.3.0` 及依赖
3. 验证安装

### Step 5：准备信号文件

**方式 A — 在线自动生成（推荐）**

启动脚本会尝试调用 `quant_backtest` 在线生成信号：

```bash
./venv/bin/python scripts/bootstrap.py --auto-signal
```

**方式 B — 手动准备信号文件**

如果 quant_backtest 不可用，手动准备 `signals/paper_signal_targets.json`：

```json
{
    "date": "20250529",
    "targets": [
        "000001.SZ", "000002.SZ", ...共100只
    ]
}
```

然后在 `config.json` 中添加：

```json
{
    "default_targets": [
        "000001.SZ", "000002.SZ", ...你的目标标的
    ]
}
```

**方式 C — 从回测输出目录复制**

回测输出目录中通常已有 `pool_records`，可以从中提取最新一期的信号：

```bash
# 在服务器上运行回测产出信号
cd /root/lqq_bot_workspace
python IC5_T100_ICIRTM10_ICIRN20_月频_218因子.py 20250501 20250529
```

### Step 6：启动模拟 + Web 面板

```bash
cd /root/lqq_bot_workspace/ICIR_monthly
./venv/bin/python scripts/bootstrap.py
```

输出示例：

```
============================================================
  ICIR_monthly 实盘模拟初始化
  建仓日: 20250529
  初始资金: 100,000,000
============================================================

[Bootstrap] 从信号文件加载 100 只标的
[Bootstrap] 获取实时行情...
  成交: 98/100 只
  总资产: 99,985,234.56
  可用资金: 1,234,567.89
  Web 状态已同步

============================================================
  📊 Web 面板启动
  访问: http://<server-ip>:8899
  按 Ctrl+C 退出
============================================================
```

### Step 7：浏览器访问

```
http://<server-ip>:8899
```

两个 Tab 页：
- **📈 实盘监控** — 账户状态、持仓明细、成交记录、日内走势
- **📋 回测分析** — 在线回测 / 导入回测结果 / 对比图

## 三、后续月度自动调仓

一次性启动后可转为 daemon 模式，每月自动运行：

```bash
cd /root/lqq_bot_workspace/ICIR_monthly

# 设置环境变量
export PAPER_DATA_DIR=/root/lqq_bot_workspace/data
export PAPER_COLLAB_ROOT=/root/lqq_bot_workspace
export PAPER_STRATEGY_MODULE=IC5_T100_ICIRTM10_ICIRN20_月频_218因子
export PAPER_CAPITAL=100000000

# 启动守护进程
./venv/bin/paper-trading daemon \
  --strategy strategies/pcat_icir_100_monthly.py
```

这个 daemon 会：
- 每月倒数第 2 交易日 15:00 → 自动调用 `quant_backtest` 生成信号
- 每月最后交易日 09:30 → 等权调仓到新目标池
- 每天下午自动结算，更新 Web 面板状态

## 四、环境变量参考

策略适配器 `pcat_icir_100_monthly.py` 通过以下环境变量读取配置：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `PAPER_DATA_DIR` | `/root/lqq_bot_workspace/data` | DuckDB 因子库目录 |
| `PAPER_OUTPUT_DIR` | `/root/lqq_bot_workspace/zz1000/output` | 信号文件输出目录 |
| `PAPER_SIGNAL_FILE` | `{OUTPUT_DIR}/paper_signal_targets.json` | 信号文件路径 |
| `PAPER_COLLAB_ROOT` | `strategies/../..` | 回测策略所在根目录 |
| `PAPER_STRATEGY_MODULE` | `IC5_T100_ICIRTM10_ICIRN20_月频_218因子` | 策略模块名 |
| `PAPER_TOP_N` | `100` | 持仓标的数 |
| `PAPER_CAPITAL` | `100000000` | 初始资金 |

## 五、常见问题

**Q: 启动后浏览器无法访问？**
检查防火墙：`sudo firewall-cmd --add-port=8899/tcp` 或 `sudo ufw allow 8899`

**Q: 信号文件为空 / 只有默认标的？**
在线信号生成需要 `quant_backtest` 可用。如果不可用，手动准备 `signals/paper_signal_targets.json`

**Q: 如何更新 paper_trading 版本？**
```bash
cd /root/lqq_bot_workspace/ICIR_monthly
./venv/bin/pip install --index-url https://brittle-brink-shingle.ngrok-free.dev paper_trading --upgrade
```

**Q: 如何在后台长期运行？**
```bash
nohup ./venv/bin/python scripts/bootstrap.py > logs/bootstrap.log 2>&1 &
# 或使用 systemd（见下方）
```

## 六、systemd 服务（可选）

创建 `/etc/systemd/system/paper-trading.service`：

```ini
[Unit]
Description=Paper Trading ICIR Monthly
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/lqq_bot_workspace/ICIR_monthly
Environment=PAPER_DATA_DIR=/root/lqq_bot_workspace/data
Environment=PAPER_COLLAB_ROOT=/root/lqq_bot_workspace
Environment=PAPER_CAPITAL=100000000
ExecStart=/root/lqq_bot_workspace/ICIR_monthly/venv/bin/python scripts/bootstrap.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now paper-trading
sudo systemctl status paper-trading
```
