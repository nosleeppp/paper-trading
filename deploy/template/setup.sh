#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# paper_trading 通用安装脚本
# 在任何策略目录下运行: bash setup.sh
# ═══════════════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "============================================"
echo "  paper_trading 安装"
echo "  目录: $SCRIPT_DIR"
echo "============================================"

# ── 1. 虚拟环境 ──────────────────────────────────
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "[1/3] 创建 Python 虚拟环境..."
    python3 -m venv "$SCRIPT_DIR/venv"
else
    echo "[1/3] 虚拟环境已存在"
fi

source "$SCRIPT_DIR/venv/bin/activate"
pip install --upgrade pip -q

# ── 2. 安装 paper_trading ────────────────────────────
echo "[2/3] 安装 paper_trading..."
pip install --index-url https://brittle-brink-shingle.ngrok-free.dev \
    "paper_trading>=0.5.0" \
    websocket-client requests openpyxl \
    -q

# ── 3. 验证 ──────────────────────────────────────
echo "[3/3] 验证安装..."
python -c "
from paper_trading import (
    PaperEngine, PaperBroker, Context, DataProvider,
    passorder, set_basket, order_algo, update_paper_state,
    OP_BUY, OP_SELL, OP_BUY_BASKET, OP_SELL_BASKET,
    ORDER_LIMIT, ORDER_MARKET, ORDER_BASKET_BY_AMOUNT,
)
print('  paper_trading', __import__('paper_trading').__version__)
print('  OK')
"

echo ""
echo "============================================"
echo "  安装完成"
echo "============================================"
echo ""
echo "下一步:"
echo "  1. 编辑 config.json"
echo "  2. 准备 signals/targets.json"
echo "  3. ./venv/bin/python scripts/bootstrap.py"
echo "  4. 浏览器访问 http://<ip>:8899"
