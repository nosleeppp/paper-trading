/**
 * 实盘模拟监控 + 回测分析
 */
const API_STATUS = '/api/status';
const API_BT_YEARS = '/api/backtest/years';
const API_BT_LOAD = '/api/backtest/load';

let paperData = null;
let backtestData = null;
let chartNavPaper, chartNavBt, chartDDBt;

document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    initTabs();
    updateClock();
    setInterval(updateClock, 1000);
    fetchPaperStatus();
    setInterval(fetchPaperStatus, 5000);
    fetchBacktestYears();
    window.addEventListener('resize', resizeAllCharts);
});

// ── 时钟 ────────────────────────────────────────────
function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleString('zh-CN');
    const h = now.getHours(), m = now.getMinutes();
    const inSession = (h === 9 && m >= 30) || (h === 10) || (h === 11 && m <= 30) || (h >= 13 && h < 15);
    const el = document.getElementById('market-status');
    el.textContent = inSession ? '交易中' : '休市';
    el.className = 'badge ' + (inSession ? 'open' : '');
}

// ── Charts ──────────────────────────────────────────
function initCharts() {
    chartNavPaper = echarts.init(document.getElementById('chart-nav-paper'));
    chartNavBt = echarts.init(document.getElementById('chart-nav-bt'));
    chartDDBt = echarts.init(document.getElementById('chart-dd-bt'));
}

function resizeAllCharts() {
    [chartNavPaper, chartNavBt, chartDDBt].forEach(c => c?.resize());
}

// ── Tabs ────────────────────────────────────────────
function initTabs() {
    document.querySelectorAll('.top-tabs .tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.top-tabs .tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
            setTimeout(resizeAllCharts, 100);
        });
    });
}

// ══════════════════════════════════════════════════════
// 实盘
// ══════════════════════════════════════════════════════
async function fetchPaperStatus() {
    try {
        const resp = await fetch(API_STATUS);
        paperData = await resp.json();
        if (paperData.account) updatePaperDashboard();
    } catch (e) { /* ok */ }
}

function updatePaperDashboard() {
    const acc = paperData.account || {};
    document.getElementById('total-value').textContent = fmtWan(acc.total_value);
    document.getElementById('capital').textContent = fmtWan(acc.capital);
    document.getElementById('pos-count').textContent = acc.position_count || 0;
    const posValue = (acc.total_value || 0) - (acc.capital || 0);
    document.getElementById('pos-value').textContent = fmtWan(Math.max(posValue, 0));
    const retEl = document.getElementById('total-return');
    const ret = acc.total_return || 0;
    retEl.textContent = (ret * 100).toFixed(2) + '%';
    retEl.className = 'card-value ' + (ret >= 0 ? 'positive' : 'negative');
    // 附加快照指标
    setIf('annual-return', acc.annual_return, 'pct');
    setIf('bench-return', acc.benchmark_return, 'pct');
    setIf('excess-return', acc.excess_return, 'pct');
    setIf('sharpe', acc.sharpe, 'num2');
    setIf('max-dd', acc.max_drawdown, 'pct');
    setIf('excess-max-dd', acc.excess_max_dd, 'pct');
    setIf('excess-sharpe', acc.excess_sharpe, 'num2');
    setIf('alpha', acc.alpha, 'num4');
    setIf('beta', acc.beta, 'num4');
    setIf('win-rate', acc.win_rate, 'pct');
    updatePositionsTable(paperData.positions || [], 'positions-table');
    updateTradesTable(paperData.orders || [], 'trades-table');
    const benchmark = paperData.benchmark_nav || [];
    updateNavChart(paperData.pnl_curve || [], chartNavPaper, benchmark);
}

function setIf(id, val, fmt) {
    const el = document.getElementById(id);
    if (!el || val == null) return;
    if (fmt === 'pct') el.textContent = (val * 100).toFixed(2) + '%';
    else if (fmt === 'num2') el.textContent = val.toFixed(2);
    else if (fmt === 'num4') el.textContent = val.toFixed(4);
    else el.textContent = val;
}

// ══════════════════════════════════════════════════════
// 回测 — 本地文件夹加载
// ══════════════════════════════════════════════════════
async function fetchBacktestYears() {
    try {
        const resp = await fetch(API_BT_YEARS);
        const years = await resp.json();
        const select = document.getElementById('bt-year-select');
        if (!select) return;
        select.innerHTML = years.map(y => `<option value="${y}">${y}</option>`).join('');
        if (years.length) {
            select.value = years[0];
            loadBacktestYear();
        }
    } catch (e) { /* ok */ }
}

async function loadBacktestYear() {
    const year = document.getElementById('bt-year-select')?.value;
    const statusEl = document.getElementById('bt-load-status');
    if (!year) return;

    statusEl.textContent = '加载中...';
    statusEl.className = 'status-msg running';

    try {
        const resp = await fetch(API_BT_LOAD + '?year=' + year);
        const data = await resp.json();
        if (data.error) {
            statusEl.textContent = data.error;
            statusEl.className = 'status-msg error';
            return;
        }
        backtestData = data;
        statusEl.textContent = '';
        statusEl.className = 'status-msg';
        updateBacktestDashboard();
    } catch (e) {
        statusEl.textContent = '加载失败: ' + e.message;
        statusEl.className = 'status-msg error';
    }
}

function updateBacktestDashboard() {
    if (!backtestData) return;
    document.getElementById('bt-results').style.display = '';

    const acc = backtestData.account || {};
    const retEl = document.getElementById('bt-return');
    const ret = acc.total_return || 0;
    retEl.textContent = (ret * 100).toFixed(2) + '%';
    retEl.className = 'card-value ' + (ret >= 0 ? 'positive' : 'negative');

    document.getElementById('bt-annual').textContent = acc.annual_return != null ? (acc.annual_return * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-bench').textContent = acc.benchmark_return != null ? (acc.benchmark_return * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-excess').textContent = acc.excess_return != null ? (acc.excess_return * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-sharpe').textContent = acc.sharpe != null ? acc.sharpe.toFixed(2) : '--';
    document.getElementById('bt-dd').textContent = acc.max_drawdown != null ? (acc.max_drawdown * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-excess-dd').textContent = acc.excess_max_dd != null ? (acc.excess_max_dd * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-excess-sharpe').textContent = acc.excess_sharpe != null ? acc.excess_sharpe.toFixed(2) : '--';
    document.getElementById('bt-alpha').textContent = acc.alpha != null ? acc.alpha.toFixed(4) : '--';
    document.getElementById('bt-beta').textContent = acc.beta != null ? acc.beta.toFixed(4) : '--';
    document.getElementById('bt-winrate').textContent = acc.win_rate != null ? (acc.win_rate * 100).toFixed(1) + '%' : '--';

    updateNavChart(backtestData.nav_series || [], chartNavBt, backtestData.benchmark_nav || []);
    updateDrawdownChart(backtestData.drawdown_series || []);
    updateTradesTable(backtestData.trades || [], 'bt-trades-table');
    populateBtTradeDates(backtestData.trades || []);
    updatePositionsTable(backtestData.positions || [], 'bt-positions-table');
}

function updateDrawdownChart(data) {
    if (!chartDDBt || !data.length) return;
    chartDDBt.setOption({
        tooltip: { trigger: 'axis', valueFormatter: v => (v * 100).toFixed(2) + '%' },
        xAxis: { type: 'category', data: data.map(d => d.date), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' } },
        series: [{
            name: '回撤', type: 'line',
            data: data.map(d => d.drawdown),
            smooth: true, lineStyle: { color: '#e74c3c', width: 2 },
            areaStyle: { color: 'rgba(231,76,60,0.15)' },
        }],
    }, true);
}

// ══════════════════════════════════════════════════════
// 通用渲染
// ══════════════════════════════════════════════════════
function updatePositionsTable(positions, tableId) {
    const tbody = document.querySelector('#' + tableId + ' tbody');
    if (!tbody) return;
    tbody.innerHTML = positions.map(p => `
        <tr><td>${p.stockcode || ''}</td><td>${p.quantity || 0}</td><td>${(p.avg_cost || 0).toFixed(2)}</td>
        <td>${(p.price || 0).toFixed(2)}</td><td>${fmtWan(p.market_value || 0)}</td>
        <td style="color:${(p.unrealized_pnl||0)>=0?'#e74c3c':'#27ae60'}">${fmtWan(p.unrealized_pnl||0)}</td></tr>
    `).join('');
}

function updateTradesTable(trades, tableId) {
    const tbody = document.querySelector('#' + tableId + ' tbody');
    if (!tbody) return;
    const isPaper = tableId === 'trades-table';
    tbody.innerHTML = trades.slice(-200).reverse().map(o => `
        <tr data-date="${(o.time || o.date || '').substring(0, 10)}" data-code="${o.stockcode || ''}">
            <td>${o.time || o.date || ''}</td><td>${o.stockcode || ''}</td>
            <td style="color:${o.side==='BUY'?'#e74c3c':'#27ae60'}">${o.side==='BUY'?'买入':'卖出'}</td>
            <td>${o.quantity || 0}</td><td>${(o.price || 0).toFixed(2)}</td>
            ${isPaper ? `<td>${fmtWan(o.amount || o.quantity * o.price)}</td>` : ''}
        </tr>
    `).join('');
    if (isPaper) populateTradeDates(trades);
}

function populateTradeDates(trades) {
    const select = document.getElementById('paper-trade-filter');
    if (!select) return;
    const dates = [...new Set(trades.map(t => (t.time || t.date || '').substring(0, 8)))].filter(Boolean).sort().reverse();
    select.innerHTML = '<option value="">全部日期</option>' +
        dates.map(d => `<option value="${d}">${d.substring(0,4)}-${d.substring(4,6)}-${d.substring(6,8)}</option>`).join('');
}

function filterPaperTrades() {
    const filter = (document.getElementById('paper-trade-filter')?.value || '').toLowerCase();
    document.querySelectorAll('#trades-table tbody tr').forEach(row => {
        row.style.display = (!filter || (row.dataset.date||'').toLowerCase().includes(filter)) ? '' : 'none';
    });
}

function filterBtTrades() {
    const filter = (document.getElementById('bt-trade-filter')?.value || '').toLowerCase();
    document.querySelectorAll('#bt-trades-table tbody tr').forEach(row => {
        const d = (row.dataset.date || '').toLowerCase();
        row.style.display = (!filter || d.includes(filter)) ? '' : 'none';
    });
}

function populateBtTradeDates(trades) {
    const select = document.getElementById('bt-trade-filter');
    if (!select) return;
    const dates = [...new Set(trades.map(t => (t.time || t.date || '').substring(0, 8)))].filter(Boolean).sort().reverse();
    select.innerHTML = '<option value="">全部日期</option>' +
        dates.map(d => `<option value="${d}">${d.substring(0,4)}-${d.substring(4,6)}-${d.substring(6,8)}</option>`).join('');
}

function updateNavChart(data, chart, benchmark) {
    if (!chart || !data.length) return;
    const series = [{
        name: '策略净值', type: 'line',
        data: data.map(d => d.nav),
        smooth: true, lineStyle: { color: '#2980b9', width: 2 },
        areaStyle: { color: 'rgba(41,128,185,0.1)' },
    }];
    if (benchmark && benchmark.length) {
        const bmMap = {};
        benchmark.forEach(b => { bmMap[b.date] = b.nav; });
        series.push({
            name: '中证1000', type: 'line',
            data: data.map(d => bmMap[d.date || d.time] || null),
            smooth: true,
            lineStyle: { color: '#95a5a6', width: 1.5, type: 'dashed' },
        });
    }
    chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: series.map(s => s.name), bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '12%', top: '3%', containLabel: true },
        xAxis: { type: 'category', data: data.map(d => d.date || d.time || ''), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: v => v.toFixed(2) } },
        series,
    }, true);
}

function fmtWan(val) {
    if (val == null || isNaN(val)) return '--';
    const num = Math.abs(val);
    if (num >= 1e8) return (val / 1e8).toFixed(2) + '亿';
    if (num >= 1e4) return (val / 1e4).toFixed(2) + '万';
    return val.toFixed(2);
}
