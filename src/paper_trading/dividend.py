"""
复权+分红调整模块 — 对齐 QMT 处理流程
=====================================
每个交易日 21:00 触发，检测持仓股票的除权除息事件。

QMT 对齐逻辑:
  1. 现金分红 → 计入可用现金
  2. 送股 → 更新持仓股数
  3. 成本价前复权: 新成本价 = 原成本价 / dr
  4. 历史交易记录和持仓快照同步前复权
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, date
from typing import Dict, List, Optional, Any

import pandas as pd

logger = logging.getLogger(__name__)


# ── 数据源连接器 ─────────────────────────────────────────────

class _DataSource:
    """统一 parquet / MySQL 数据读取。"""

    def __init__(self, config: dict):
        self.source = config.get('source', 'parquet')
        self.parquet_path = config.get('parquet_path', '')
        self.mysql_config = config.get('mysql', {})

    def read(self) -> pd.DataFrame:
        if self.source == 'parquet':
            if not self.parquet_path:
                raise FileNotFoundError(
                    "adj_factor/dividend_detail 的 parquet_path 未配置。\n"
                    "请在 config.json → dividend → adj_factor (或 dividend_detail) 中设置:\n"
                    '  "source": "parquet", "parquet_path": "/实际路径/xxx.parquet"\n'
                    '或切换为 MySQL:\n'
                    '  "source": "mysql", "mysql": {"host":"...","user":"...","password":"...","database":"...","table":"..."}'
                )
            if not os.path.exists(self.parquet_path):
                raise FileNotFoundError(
                    f"parquet 文件不存在: {self.parquet_path}\n"
                    f"请在 config.json → dividend → adj_factor (或 dividend_detail) 中修正 parquet_path,\n"
                    f"或切换为 MySQL 数据源 (source: mysql)。"
                )
            return pd.read_parquet(self.parquet_path)

        elif self.source == 'mysql':
            mc = self.mysql_config
            required = ['host', 'user', 'password', 'database', 'table']
            missing = [k for k in required if not mc.get(k)]
            if missing:
                raise ValueError(
                    f"MySQL 连接信息不完整，缺少: {missing}。\n"
                    f"请在 config.json → dividend → adj_factor (或 dividend_detail) → mysql 中填写。"
                )
            try:
                import pymysql
            except ImportError:
                raise ImportError("pymysql 未安装。pip install pymysql")
            conn = pymysql.connect(
                host=mc['host'], port=mc.get('port', 3306),
                user=mc['user'], password=mc['password'],
                database=mc['database'],
            )
            df = pd.read_sql(f"SELECT * FROM {mc['table']}", conn)
            conn.close()
            return df

        raise ValueError(f"不支持的数据源类型: {self.source}，请使用 'parquet' 或 'mysql'")


# ── 复权调整器 ───────────────────────────────────────────────

class DividendAdjuster:
    """
    复权+分红调整器。

    用法:
        adjuster = DividendAdjuster(store, adj_factor_cfg, dividend_cfg)
        events = adjuster.check_events()
        for e in events:
            adjuster.apply_event(e)
    """

    def __init__(self, store, adj_factor_config: dict, dividend_config: dict):
        self._store = store
        self._adj_source = _DataSource(adj_factor_config)
        self._div_source = _DataSource(dividend_config)
        self._adj_df: Optional[pd.DataFrame] = None
        self._div_df: Optional[pd.DataFrame] = None

    def _ensure_data(self):
        if self._adj_df is None:
            self._adj_df = self._adj_source.read()
            self._adj_df['trade_date'] = self._adj_df['trade_date'].astype(str).str[:8]
            self._adj_df['adj_factor'] = self._adj_df['adj_factor'].astype(float)
        if self._div_df is None:
            self._div_df = self._div_source.read()

    def check_events(self) -> List[dict]:
        """
        扫描持仓，返回需要处理的分红事件。

        主检测逻辑：adj_factor 变动（任何复权因子跳变 = 除权事件）。
        dividend_detail 作为补充数据（现金分红/送股明细），不是必要条件。
        """
        self._ensure_data()
        positions = self._store.get_positions()
        if not positions:
            return []

        # 获取每只股票的最早买入日期
        first_buy_dates = {}
        order_rows = self._store._get_conn().execute(
            "SELECT stockcode, MIN(trade_date) FROM orders WHERE side='BUY' GROUP BY stockcode"
        ).fetchall()
        for code, d in order_rows:
            first_buy_dates[code] = str(d)[:8]

        events = []

        for code, pos in positions.items():
            qty = int(pos.get('quantity', 0))
            if qty <= 0:
                continue

            # 主检测：从 adj_factor 找变动点
            adj_rows = self._adj_df[self._adj_df['ts_code'] == code].sort_values('trade_date')
            if len(adj_rows) < 2:
                continue

            first_buy = first_buy_dates.get(code, '19700101')
            prev_adj = None
            prev_date = None

            for _, row in adj_rows.iterrows():
                curr_date = str(row['trade_date'])[:8]
                curr_adj = float(row['adj_factor'])

                if prev_adj is not None and abs(curr_adj - prev_adj) > 0.0001:
                    # adj_factor 发生变动 → 除权事件
                    dr = curr_adj / prev_adj if prev_adj > 0 else 1.0
                    # 变动日 = 当前行日期（除权除息日）
                    ex_date_str = curr_date

                    if ex_date_str <= first_buy:
                        prev_adj = curr_adj; prev_date = curr_date
                        continue

                    # 查是否已处理
                    already = self._store._get_conn().execute(
                        "SELECT COUNT(*) FROM dividend_log WHERE stockcode=? AND ex_date=?",
                        (code, ex_date_str)
                    ).fetchone()[0]
                    if already:
                        prev_adj = curr_adj; prev_date = curr_date
                        continue

                    # 从 dividend_detail 补充信息（可选）
                    cash_per_share = 0.0
                    stk_ratio = 0.0
                    if self._div_df is not None and not self._div_df.empty:
                        div_match = self._div_df[
                            (self._div_df['ts_code'] == code) &
                            (self._div_df['ex_date'].notna())
                        ]
                        if not div_match.empty:
                            div_match['ex_str'] = div_match['ex_date'].astype(str).str[:10].str.replace('-', '')[:8]
                            row_match = div_match[div_match['ex_str'] == ex_date_str]
                            if not row_match.empty:
                                r = row_match.iloc[0]
                                cash_per_share = float(r.get('cash_div_tax', 0) or 0)
                                stk_ratio = float(r.get('stk_div', 0) or 0)

                    events.append({
                        'stockcode': code, 'ex_date': ex_date_str,
                        'dr': dr, 'cash_div_per_share': cash_per_share,
                        'stk_div_ratio': stk_ratio,
                        'shares_before': qty,
                        'cost_before': float(pos.get('avg_cost', 0)),
                        'prev_adj': prev_adj, 'curr_adj': curr_adj,
                    })

                prev_adj = curr_adj
                prev_date = curr_date

        return sorted(events, key=lambda e: e['ex_date'])

    def apply_event(self, event: dict) -> dict:
        """
        处理单个分红事件。返回调整结果。

        执行顺序（QMT 一致）:
        1. 送股 → 更新持仓股数
        2. 前复权成本价
        3. 现金分红入账
        4. 历史记录前复权
        5. 写入日志
        """
        code = event['stockcode']
        ex_date = event['ex_date']
        dr = event['dr']
        cash_per_share = event['cash_div_per_share']
        stk_ratio = event['stk_div_ratio']
        qty_before = event['shares_before']
        cost_before = event['cost_before']
        conn = self._store._get_conn()

        result = {
            'stockcode': code, 'ex_date': ex_date,
            'shares_before': qty_before, 'cost_before': cost_before,
            'dr': dr, 'cash_per_share': cash_per_share, 'stk_ratio': stk_ratio,
        }

        # ── 1. 送股 ──
        if stk_ratio > 0:
            new_qty = int(qty_before * (1 + stk_ratio))
            conn.execute("UPDATE positions SET quantity=?, available=? WHERE stockcode=?",
                         (new_qty, new_qty, code))
            conn.commit()
            result['shares_after'] = new_qty
            logger.info("[Dividend] %s 送股: %d → %d 股 (10送%.0f)",
                       code, qty_before, new_qty, stk_ratio * 10)
        else:
            result['shares_after'] = qty_before

        # ── 2. 前复权成本价 ──
        new_cost = cost_before / dr if dr > 0 else cost_before
        conn.execute("UPDATE positions SET avg_cost=? WHERE stockcode=?", (new_cost, code))
        conn.commit()
        result['cost_after'] = new_cost
        logger.info("[Dividend] %s 成本价: %.2f → %.2f (dr=%.4f)", code, cost_before, new_cost, dr)

        # ── 3. 现金分红入账 ──
        cash_added = 0.0
        if cash_per_share > 0:
            cash_added = qty_before * cash_per_share
            acc = self._store.get_account()
            old_cash = acc.get('cash', acc.get('capital', 0))
            new_cash = old_cash + cash_added
            self._store.save_account({
                'cash': new_cash,
                'total_value': (acc.get('total_value', 0) or old_cash) + cash_added,
                'initial_capital': acc.get('initial_capital', 0),
                'total_return': acc.get('total_return', 0),
                'position_count': acc.get('position_count', 0),
            })
            conn.commit()
            result['cash_added'] = cash_added
            logger.info("[Dividend] %s 现金分红: %.2f 元 (%.4f/股 × %d股)",
                       code, cash_added, cash_per_share, qty_before)
        else:
            result['cash_added'] = 0.0

        # ── 4. 历史交易记录前复权 ──
        conn.execute(
            "UPDATE orders SET price=price/?, amount=quantity*price WHERE stockcode=? AND trade_date<?",
            (dr, code, ex_date)
        )
        conn.commit()
        logger.info("[Dividend] %s 历史成交价已前复权", code)

        # ── 5. 持仓快照历史前复权 ──
        conn.execute(
            "UPDATE position_snapshots SET price=price/?, market_value=quantity*price WHERE stockcode=? AND date<?",
            (dr, code, ex_date)
        )
        conn.commit()
        logger.info("[Dividend] %s 历史持仓快照已前复权", code)

        # ── 6. 日志 ──
        conn.execute(
            "INSERT INTO dividend_log (stockcode, ex_date, cash_per_share, stock_div_ratio, "
            "dr_factor, shares_before, cost_before, cost_after, cash_added) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, ex_date, cash_per_share, stk_ratio, dr,
             qty_before, cost_before, new_cost, cash_added)
        )
        conn.commit()

        self._store.flush()
        return result
