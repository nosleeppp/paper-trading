# paper_trading 通用部署模板

复制此目录到你的策略工作区，按需修改配置即可启动。

## 目录

```
your-strategy/
├── config.json          ← 编辑
├── setup.sh             ← 运行安装
├── README.md
├── venv/                ← 自动创建
├── scripts/
│   └── bootstrap.py     ← 启动脚本
├── strategies/          ← 放你的策略适配器
└── signals/             ← 信号文件
    └── targets.json
```

## 快速开始

```bash
# 1. 编辑 config.json（修改 capital, start_date, default_targets）
vim config.json

# 2. 安装
bash setup.sh

# 3. 准备信号文件 signals/targets.json，或使用 config.json 中的 default_targets

# 4. 启动
./venv/bin/python scripts/bootstrap.py

# 5. 访问 http://<ip>:8899
```
