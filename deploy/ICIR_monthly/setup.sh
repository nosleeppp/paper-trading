#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# ICIR_monthly 实盘模拟 — 一键安装脚本
# ═══════════════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "============================================"
echo "  ICIR_monthly 实盘模拟安装"
echo "  目标目录: $SCRIPT_DIR"
echo "============================================"

# ── 1. 创建虚拟环境 ──────────────────────────────────
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "[1/4] 创建 Python 虚拟环境..."
    python3 -m venv "$SCRIPT_DIR/venv"
else
    echo "[1/4] 虚拟环境已存在，跳过"
fi

source "$SCRIPT_DIR/venv/bin/activate"

# ── 2. 安装 paper_trading ────────────────────────────
echo "[2/4] 安装 paper_trading..."
pip install --upgrade pip -q
pip install --index-url https://brittle-brink-shingle.ngrok-free.dev \
    "paper_trading>=0.3.0" \
    akshare \
    websocket-client \
    requests \
    -q

# ── 3. 验证安装 ──────────────────────────────────────
echo "[3/4] 验证安装..."
python -c "
from paper_trading import (
    PaperEngine, PaperBroker, Context,
    passorder, set_basket, order_algo,
    OP_BUY_BASKET, ORDER_BASKET_BY_AMOUNT,
    update_paper_state,
)
print('  paper_trading', __import__('paper_trading').__version__)
print('  所有模块导入成功')
"

# ── 4. 确认配置文件 ──────────────────────────────────
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo "[4/4] ⚠ 未找到 config.json，请先编辑配置！"
    cp "$SCRIPT_DIR/config.json.example" "$SCRIPT_DIR/config.json" 2>/dev/null || true
else
    echo "[4/4] config.json 已就绪"
fi

echo ""
echo "============================================"
echo "  ✅ 安装完成"
echo "============================================"
echo ""
echo "下一步："
echo "  1. 编辑 config.json 修改数据路径"
echo "  2. 运行: ./venv/bin/python scripts/bootstrap.py"
echo "  3. 浏览器访问 http://<server-ip>:8899"
echo ""
echo "或使用 systemd 守护:"
echo "  sudo cp paper-trading.service /etc/systemd/system/"
echo "  sudo systemctl enable --now paper-trading"
