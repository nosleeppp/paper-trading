/**
 * 实盘模拟监控 + 回测分析
 */
const API_STATUS = '/api/status';
const API_BT_RUN = '/api/backtest/run';
const API_BT_RESULT = '/api/backtest/result';
const API_BT_UPLOAD = '/api/backtest/upload';

let paperData = null;
let backtestData = null;
let currentTaskId = null;
let pollTimer = null;

let chartNavPaper, chartIntraday, chartNavBt, chartDDBt, chartCompare;

document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    initTabs();
    updateClock();
    setInterval(updateClock, 1000);
    fetchPaperStatus();
    setInterval(fetchPaperStatus, 5000);
    window.addEventListener('resize', resizeAllCharts);
    const today = new Date();
    document.getElementById('bt-end').value = today.toISOString().split('T')[0];
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
    chartIntraday = echarts.init(document.getElementById('chart-intraday'));
    chartNavBt = echarts.init(document.getElementById('chart-nav-bt'));
    chartDDBt = echarts.init(document.getElementById('chart-dd-bt'));
    chartCompare = echarts.init(document.getElementById('chart-compare'));
}

function resizeAllCharts() {
    [chartNavPaper, chartIntraday, chartNavBt, chartDDBt, chartCompare].forEach(c => c?.resize());
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
    document.querySelectorAll('.sub-tabs .sub-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.sub-tabs .sub-tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            document.querySelectorAll('.subtab-content').forEach(t => t.classList.remove('active'));
            document.getElementById('subtab-' + btn.dataset.subtab).classList.add('active');
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
        tryUpdateCompare();
    } catch (e) { /* ok */ }
}

function updatePaperDashboard() {
    const acc = paperData.account || {};
    document.getElementById('total-value').textContent = fmtWan(acc.total_value);
    document.getElementById('capital').textContent = fmtWan(acc.capital);
    const retEl = document.getElementById('total-return');
    const ret = acc.total_return || 0;
    retEl.textContent = (ret * 100).toFixed(2) + '%';
    retEl.className = 'card-value ' + (ret >= 0 ? 'positive' : 'negative');
    document.getElementById('pos-count').textContent = acc.position_count || 0;
    updatePositionsTable(paperData.positions || [], 'positions-table');
    updateTradesTable(paperData.orders || [], 'trades-table');
    updateNavChart(paperData.pnl_curve || [], chartNavPaper);
    updateIntradayChart(paperData.intraday || []);
}

// ══════════════════════════════════════════════════════
// 在线回测
// ══════════════════════════════════════════════════════
async function runBacktest() {
    const startEl = document.getElementById('bt-start');
    const endEl = document.getElementById('bt-end');
    const capitalEl = document.getElementById('bt-capital');
    const strategyEl = document.getElementById('bt-strategy');
    const pythonpathEl = document.getElementById('bt-pythonpath');
    const dataDirEl = document.getElementById('bt-data-dir');
    const statusEl = document.getElementById('bt-run-status');
    const progressEl = document.getElementById('bt-run-progress');

    statusEl.textContent = '正在启动回测...';
    statusEl.className = 'status-msg running';
    progressEl.style.display = 'block';

    try {
        const resp = await fetch(API_BT_RUN, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                start_date: startEl.value.replace(/-/g, ''),
                end_date: endEl.value.replace(/-/g, ''),
                capital: parseFloat(capitalEl.value) * 10000,
                strategy_module: strategyEl.value || null,
                pythonpath: pythonpathEl.value || null,
                data_dir: dataDirEl.value || null,
            }),
        });
        const data = await resp.json();
        currentTaskId = data.task_id;
        statusEl.textContent = '回测运行中...';
        progressEl.querySelector('.progress-fill').style.width = '20%';
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollBacktestResult, 1500);
    } catch (e) {
        statusEl.textContent = '请求失败: ' + e.message;
        statusEl.className = 'status-msg error';
        progressEl.style.display = 'none';
    }
}

async function pollBacktestResult() {
    if (!currentTaskId) return;
    try {
        const resp = await fetch(API_BT_RESULT + '/' + currentTaskId);
        const data = await resp.json();
        const statusEl = document.getElementById('bt-run-status');
        const progressEl = document.getElementById('bt-run-progress');

        if (data.status === 'done') {
            clearInterval(pollTimer); pollTimer = null;
            statusEl.textContent = '回测完成';
            statusEl.className = 'status-msg done';
            progressEl.querySelector('.progress-fill').style.width = '100%';
            setTimeout(() => { progressEl.style.display = 'none'; }, 500);
            backtestData = data.result;
            updateBacktestDashboard();
            tryUpdateCompare();
        } else if (data.status === 'error') {
            clearInterval(pollTimer); pollTimer = null;
            statusEl.textContent = '回测失败: ' + (data.error || '');
            statusEl.className = 'status-msg error';
            progressEl.style.display = 'none';
        } else {
            progressEl.querySelector('.progress-fill').style.width = '60%';
        }
    } catch (e) { /* keep polling */ }
}

// ══════════════════════════════════════════════════════
// 导入回测结果（文件上传）
// ══════════════════════════════════════════════════════
async function uploadBacktestFiles() {
    const statusEl = document.getElementById('bt-upload-status');
    const formData = new FormData();

    const files = {
        'trades': 'bt-file-trades',
        'positions': 'bt-file-positions',
        'nav': 'bt-file-nav',
        'summary': 'bt-file-summary',
    };

    let hasFile = false;
    for (const [key, elId] of Object.entries(files)) {
        const el = document.getElementById(elId);
        if (el && el.files && el.files[0]) {
            formData.append(key, el.files[0]);
            hasFile = true;
        }
    }

    if (!hasFile) {
        statusEl.textContent = '请至少选择一个文件';
        statusEl.className = 'status-msg error';
        return;
    }

    statusEl.textContent = '正在上传并解析...';
    statusEl.className = 'status-msg running';

    try {
        const resp = await fetch(API_BT_UPLOAD, { method: 'POST', body: formData });
        const data = await resp.json();

        if (data.success) {
            const p = data.parsed || {};
            statusEl.textContent = `解析完成: ${p.nav_days || 0}天净值, ${p.trades || 0}笔交易, ${p.positions || 0}只持仓`;
            statusEl.className = 'status-msg done';

            // 从 result API 获取完整数据
            const rResp = await fetch(API_BT_RESULT + '/' + data.task_id);
            const rData = await rResp.json();
            if (rData.status === 'done') {
                backtestData = rData.result;
                updateBacktestDashboard();
                tryUpdateCompare();
            }
        } else {
            statusEl.textContent = '解析失败: ' + (data.error || '未知错误');
            statusEl.className = 'status-msg error';
        }
    } catch (e) {
        statusEl.textContent = '上传失败: ' + e.message;
        statusEl.className = 'status-msg error';
    }
}

// ══════════════════════════════════════════════════════
// 回测结果渲染
// ══════════════════════════════════════════════════════
function updateBacktestDashboard() {
    if (!backtestData) return;
    document.getElementById('bt-results').style.display = '';

    const acc = backtestData.account || {};
    const retEl = document.getElementById('bt-return');
    const ret = acc.total_return || 0;
    retEl.textContent = (ret * 100).toFixed(2) + '%';
    retEl.className = 'card-value ' + (ret >= 0 ? 'positive' : 'negative');

    document.getElementById('bt-annual').textContent = acc.annual_return ? (acc.annual_return * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-sharpe').textContent = acc.sharpe ? acc.sharpe.toFixed(2) : '--';
    document.getElementById('bt-dd').textContent = acc.max_drawdown ? (acc.max_drawdown * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-alpha').textContent = acc.alpha ? acc.alpha.toFixed(4) : '--';
    document.getElementById('bt-winrate').textContent = acc.win_rate ? (acc.win_rate * 100).toFixed(1) + '%' : '--';

    // 净值图
    updateNavChart(backtestData.nav_series || [], chartNavBt);

    // 回撤图
    const ddData = backtestData.drawdown_series || [];
    if (ddData.length && chartDDBt) {
        chartDDBt.setOption({
            tooltip: { trigger: 'axis', valueFormatter: v => (v * 100).toFixed(2) + '%' },
            xAxis: { type: 'category', data: ddData.map(d => d.date), axisLabel: { rotate: 30, fontSize: 10 } },
            yAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' } },
            series: [{
                name: '回撤', type: 'line',
                data: ddData.map(d => d.drawdown),
                smooth: true, lineStyle: { color: '#e74c3c', width: 2 },
                areaStyle: { color: 'rgba(231,76,60,0.15)' },
            }],
        }, true);
    }

    // 成交表
    updateTradesTable(backtestData.trades || [], 'bt-trades-table');

    // 持仓表
    updatePositionsTable(backtestData.positions || [], 'bt-positions-table');
}

// ══════════════════════════════════════════════════════
// 对比图
// ══════════════════════════════════════════════════════
function tryUpdateCompare() {
    if (!paperData || !backtestData) return;
    const paperNav = (paperData.pnl_curve || []).filter(d => d.nav != null);
    const btNav = (backtestData.nav_series || []).filter(d => d.nav != null);
    if (!paperNav.length || !btNav.length) return;

    document.getElementById('compare-section').style.display = '';
    const paperDates = paperNav.map(d => d.date || d.time || '');
    chartCompare.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['实盘净值', '回测净值'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '10%', top: '5%', containLabel: true },
        xAxis: { type: 'category', data: paperDates, axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: v => v.toFixed(2) } },
        series: [
            { name: '实盘净值', type: 'line', data: paperNav.map(d => d.nav), smooth: true, lineStyle: { color: '#2980b9', width: 2 } },
            { name: '回测净值', type: 'line', data: btNav.map(d => d.nav), smooth: true, lineStyle: { color: '#e74c3c', width: 2, type: 'dashed' } },
        ],
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
        <tr data-date="${o.time || o.date || ''}" data-code="${o.stockcode || ''}">
            <td>${o.time || o.date || ''}</td><td>${o.stockcode || ''}</td>
            <td style="color:${o.side==='BUY'?'#e74c3c':'#27ae60'}">${o.side==='BUY'?'买入':'卖出'}</td>
            <td>${o.quantity || 0}</td><td>${(o.price || 0).toFixed(2)}</td>
            ${isPaper ? `<td>${fmtWan(o.amount || o.quantity * o.price)}</td>` : ''}
        </tr>
    `).join('');
}

function filterPaperTrades() {
    const filter = (document.getElementById('paper-trade-filter')?.value || '').toLowerCase();
    document.querySelectorAll('#trades-table tbody tr').forEach(row => {
        const d = (row.dataset.date || '').toLowerCase();
        const c = (row.dataset.code || '').toLowerCase();
        row.style.display = (!filter || d.includes(filter) || c.includes(filter)) ? '' : 'none';
    });
}

function filterBtTrades() {
    const filter = (document.getElementById('bt-trade-filter')?.value || '').toLowerCase();
    document.querySelectorAll('#bt-trades-table tbody tr').forEach(row => {
        const d = (row.dataset.date || '').toLowerCase();
        const c = (row.dataset.code || '').toLowerCase();
        row.style.display = (!filter || d.includes(filter) || c.includes(filter)) ? '' : 'none';
    });
}

function updateNavChart(data, chart) {
    if (!chart || !data.length) return;
    chart.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: data.map(d => d.date || d.time || ''), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: v => v.toFixed(2) } },
        series: [{ name: '净值', type: 'line', data: data.map(d => d.nav), smooth: true, lineStyle: { color: '#2980b9', width: 2 }, areaStyle: { color: 'rgba(41,128,185,0.1)' } }],
    }, true);
}

function updateIntradayChart(data) {
    if (!chartIntraday || !data.length) return;
    chartIntraday.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: data.map(d => d.time || ''), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value' },
        series: [
            { name: '总资产', type: 'line', data: data.map(d => d.total_value), smooth: true, lineStyle: { color: '#27ae60', width: 2 }, areaStyle: { color: 'rgba(39,174,96,0.1)' } },
            { name: '可用资金', type: 'line', data: data.map(d => d.capital), smooth: true, lineStyle: { color: '#e67e22', width: 1, type: 'dashed' } },
        ],
    }, true);
}

function fmtWan(val) {
    if (val == null || isNaN(val)) return '--';
    const num = Math.abs(val);
    if (num >= 1e8) return (val / 1e8).toFixed(2) + '亿';
    if (num >= 1e4) return (val / 1e4).toFixed(2) + '万';
    return val.toFixed(2);
}
