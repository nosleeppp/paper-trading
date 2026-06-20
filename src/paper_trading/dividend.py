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
            if not self.parquet_path or not os.path.exists(self.parquet_path):
                raise FileNotFoundError(f"parquet 文件不存在: {self.parquet_path}")
            return pd.read_parquet(self.parquet_path)

        elif self.source == 'mysql':
            mc = self.mysql_config
            try:
                import pymysql
                conn = pymysql.connect(
                    host=mc['host'], port=mc.get('port', 3306),
                    user=mc['user'], password=mc['password'],
                    database=mc['database'],
                )
                df = pd.read_sql(f"SELECT * FROM {mc['table']}", conn)
                conn.close()
                return df
            except ImportError:
                raise ImportError("pymysql 未安装，无法连接 MySQL。pip install pymysql")

        raise ValueError(f"不支持的数据源类型: {self.source}")


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
        每个事件包含: {stockcode, ex_date, dr, cash_div_per_share, stk_div_ratio, shares_before, cost_before}
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

            # 查该股票的除权除息事件
            div_rows = self._div_df[self._div_df['ts_code'] == code].copy()
            if div_rows.empty:
                continue

            # 只处理 div_proc == '实施方案' 且 ex_date 已确定的事件
            div_rows = div_rows[
                (div_rows['div_proc'] == '实施方案') &
                (div_rows['ex_date'].notna())
            ]
            if div_rows.empty:
                continue

            # 查复权因子变动
            adj_rows = self._adj_df[self._adj_df['ts_code'] == code].sort_values('trade_date')
            if len(adj_rows) < 2:
                continue

            first_buy = first_buy_dates.get(code, '19700101')

            for _, div in div_rows.iterrows():
                ex_date_str = str(div['ex_date'])[:10].replace('-', '')[:8]
                if ex_date_str <= first_buy:
                    continue  # 持仓前发生的，跳过

                # 查除权日前后复权因子
                ex_idx = adj_rows[adj_rows['trade_date'] == ex_date_str].index
                if len(ex_idx) == 0:
                    continue
                ex_pos = adj_rows.index.get_loc(ex_idx[0])
                if ex_pos == 0:
                    continue
                prev_adj = float(adj_rows.iloc[ex_pos - 1]['adj_factor'])
                curr_adj = float(adj_rows.iloc[ex_pos]['adj_factor'])
                dr = curr_adj / prev_adj if prev_adj > 0 else 1.0

                # 检查是否已经处理过（避免重复）
                already = self._store._get_conn().execute(
                    "SELECT COUNT(*) FROM dividend_log WHERE stockcode=? AND ex_date=?",
                    (code, ex_date_str)
                ).fetchone()[0]
                if already:
                    continue

                events.append({
                    'stockcode': code,
                    'ex_date': ex_date_str,
                    'dr': dr,
                    'cash_div_per_share': float(div.get('cash_div_tax', 0) or 0),
                    'stk_div_ratio': float(div.get('stk_div', 0) or 0),
                    'shares_before': qty,
                    'cost_before': float(pos.get('avg_cost', 0)),
                    'prev_adj': prev_adj,
                    'curr_adj': curr_adj,
                })

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
            result['shares_after'] = new_qty
            logger.info("[Dividend] %s 送股: %d → %d 股 (10送%.0f)",
                       code, qty_before, new_qty, stk_ratio * 10)
        else:
            result['shares_after'] = qty_before

        # ── 2. 前复权成本价 ──
        new_cost = cost_before / dr if dr > 0 else cost_before
        conn.execute("UPDATE positions SET avg_cost=? WHERE stockcode=?", (new_cost, code))
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
        logger.info("[Dividend] %s 历史成交价已前复权", code)

        # ── 5. 持仓快照历史前复权 ──
        conn.execute(
            "UPDATE position_snapshots SET price=price/?, market_value=quantity*price WHERE stockcode=? AND date<?",
            (dr, code, ex_date)
        )
        logger.info("[Dividend] %s 历史持仓快照已前复权", code)

        # ── 6. 日志 ──
        conn.execute(
            "INSERT INTO dividend_log (stockcode, ex_date, cash_per_share, stock_div_ratio, "
            "dr_factor, shares_before, cost_before, cost_after, cash_added) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, ex_date, cash_per_share, stk_ratio, dr,
             qty_before, cost_before, new_cost, cash_added)
        )

        self._store.flush()
        return result
