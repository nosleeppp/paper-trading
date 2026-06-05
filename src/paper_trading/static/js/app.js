/**
 * 实盘模拟监控 + 回测分析 — 前端
 */
const API_STATUS = '/api/status';
const API_BT_RUN = '/api/backtest/run';
const API_BT_RESULT = '/api/backtest/result';
const API_BT_UPLOAD = '/api/backtest/upload';

// ── 全局状态 ──────────────────────────────────────────
let paperData = null;
let backtestData = null;
let currentTaskId = null;
let pollTimer = null;

// ECharts 实例
let chartNavPaper, chartIntraday, chartNavBt, chartCompare;

document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    initTabs();
    updateClock();
    setInterval(updateClock, 1000);
    fetchPaperStatus();
    setInterval(fetchPaperStatus, 5000);
    window.addEventListener('resize', resizeAllCharts);
    // 默认回测日期
    const today = new Date();
    document.getElementById('bt-end').value = today.toISOString().split('T')[0];
});

// ── 时钟 ─────────────────────────────────────────────

function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleString('zh-CN');
    const h = now.getHours(), m = now.getMinutes();
    const inSession = (h === 9 && m >= 30) || (h === 10) || (h === 11 && m <= 30) ||
                      (h >= 13 && h < 15);
    const el = document.getElementById('market-status');
    el.textContent = inSession ? '交易中' : '休市';
    el.className = 'badge ' + (inSession ? 'open' : '');
}

// ── ECharts 初始化 ────────────────────────────────────

function initCharts() {
    chartNavPaper = echarts.init(document.getElementById('chart-nav-paper'));
    chartIntraday = echarts.init(document.getElementById('chart-intraday'));
    chartNavBt = echarts.init(document.getElementById('chart-nav-bt'));
    chartCompare = echarts.init(document.getElementById('chart-compare'));
}

function resizeAllCharts() {
    [chartNavPaper, chartIntraday, chartNavBt, chartCompare].forEach(c => c?.resize());
}

// ── Tab 切换 ─────────────────────────────────────────

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

// ═══════════════════════════════════════════════════════
// 实盘数据
// ═══════════════════════════════════════════════════════

async function fetchPaperStatus() {
    try {
        const resp = await fetch(API_STATUS);
        paperData = await resp.json();
        if (paperData.account) updatePaperDashboard();
        tryUpdateCompare();
    } catch (e) { /* 等待服务器启动 */ }
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

// ═══════════════════════════════════════════════════════
// 在线回测
// ═══════════════════════════════════════════════════════

async function runBacktest() {
    const startEl = document.getElementById('bt-start');
    const endEl = document.getElementById('bt-end');
    const capitalEl = document.getElementById('bt-capital');
    const strategyEl = document.getElementById('bt-strategy');
    const statusEl = document.getElementById('bt-run-status');
    const progressEl = document.getElementById('bt-run-progress');

    statusEl.textContent = '⏳ 正在启动回测...';
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
            }),
        });
        const data = await resp.json();
        currentTaskId = data.task_id;

        statusEl.textContent = '🔄 回测运行中...';
        progressEl.querySelector('.progress-fill').style.width = '20%';

        // 轮询结果
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(pollBacktestResult, 1500);
    } catch (e) {
        statusEl.textContent = '❌ 请求失败: ' + e.message;
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
            clearInterval(pollTimer);
            pollTimer = null;
            statusEl.textContent = '✅ 回测完成';
            statusEl.className = 'status-msg done';
            progressEl.querySelector('.progress-fill').style.width = '100%';
            setTimeout(() => { progressEl.style.display = 'none'; }, 500);

            backtestData = data.result;
            updateBacktestDashboard();
            tryUpdateCompare();
        } else if (data.status === 'error') {
            clearInterval(pollTimer);
            pollTimer = null;
            statusEl.textContent = '❌ 回测失败: ' + (data.error || '未知错误');
            statusEl.className = 'status-msg error';
            progressEl.style.display = 'none';
        } else {
            // running / pending
            const w = data.status === 'running' ? '60%' : '20%';
            progressEl.querySelector('.progress-fill').style.width = w;
        }
    } catch (e) {
        // keep polling
    }
}

// ═══════════════════════════════════════════════════════
// 导入回测结果
// ═══════════════════════════════════════════════════════

function uploadBacktestFile(event) {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = function(e) {
        document.getElementById('bt-json-input').value = e.target.result;
    };
    reader.readAsText(file);
}

async function uploadBacktestJSON() {
    const textarea = document.getElementById('bt-json-input');
    const statusEl = document.getElementById('bt-upload-status');

    let data;
    try {
        data = JSON.parse(textarea.value);
    } catch (e) {
        statusEl.textContent = '❌ JSON 格式错误: ' + e.message;
        statusEl.className = 'status-msg error';
        return;
    }

    // 支持嵌套格式：整个对象 或 包含 result 字段的包装
    if (data.result && typeof data.result === 'object') {
        data = data.result;
    }

    try {
        const resp = await fetch(API_BT_UPLOAD, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        const respData = await resp.json();
        if (respData.success) {
            statusEl.textContent = '✅ 导入成功';
            statusEl.className = 'status-msg done';
            // 直接使用前端解析的数据
            backtestData = data;
            updateBacktestDashboard();
            tryUpdateCompare();
        } else {
            statusEl.textContent = '❌ 导入失败: ' + (respData.error || '未知错误');
            statusEl.className = 'status-msg error';
        }
    } catch (e) {
        statusEl.textContent = '❌ 上传失败: ' + e.message;
        statusEl.className = 'status-msg error';
    }
}

// ═══════════════════════════════════════════════════════
// 回测结果渲染
// ═══════════════════════════════════════════════════════

function updateBacktestDashboard() {
    if (!backtestData) return;
    document.getElementById('bt-results').style.display = '';

    const acc = backtestData.account || {};

    const retEl = document.getElementById('bt-return');
    const ret = acc.total_return || 0;
    retEl.textContent = (ret * 100).toFixed(2) + '%';
    retEl.className = 'card-value ' + (ret >= 0 ? 'positive' : 'negative');

    document.getElementById('bt-annual').textContent =
        acc.annual_return ? (acc.annual_return * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-sharpe').textContent =
        acc.sharpe ? acc.sharpe.toFixed(2) : '--';
    document.getElementById('bt-dd').textContent =
        acc.max_drawdown ? (acc.max_drawdown * 100).toFixed(2) + '%' : '--';
    document.getElementById('bt-winrate').textContent =
        acc.win_rate ? (acc.win_rate * 100).toFixed(1) + '%' : '--';
    document.getElementById('bt-trade-count').textContent =
        (backtestData.trades || []).length || '--';

    // 净值图
    updateNavChart(backtestData.nav_series || [], chartNavBt);

    // 成交表
    updateTradesTable(backtestData.trades || [], 'bt-trades-table');
}

// ═══════════════════════════════════════════════════════
// 对比图
// ═══════════════════════════════════════════════════════

function tryUpdateCompare() {
    if (!paperData || !backtestData) return;

    const paperNav = (paperData.pnl_curve || []).filter(d => d.nav != null);
    const btNav = (backtestData.nav_series || []).filter(d => d.nav != null);

    if (!paperNav.length || !btNav.length) return;

    document.getElementById('compare-section').style.display = '';

    // 将回测净值对齐到实盘的第一天
    const paperDates = paperNav.map(d => d.date || d.time || '');
    const btDates = btNav.map(d => d.date || '');

    chartCompare.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['实盘净值', '回测净值'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '10%', top: '5%', containLabel: true },
        xAxis: { type: 'category', data: paperDates, axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: v => v.toFixed(2) } },
        series: [
            {
                name: '实盘净值', type: 'line',
                data: paperNav.map(d => d.nav),
                smooth: true,
                lineStyle: { color: '#2980b9', width: 2 },
                itemStyle: { color: '#2980b9' },
            },
            {
                name: '回测净值', type: 'line',
                data: btNav.map(d => d.nav),
                smooth: true,
                lineStyle: { color: '#e74c3c', width: 2, type: 'dashed' },
                itemStyle: { color: '#e74c3c' },
            },
        ],
    }, true);
}

// ═══════════════════════════════════════════════════════
// 通用渲染函数
// ═══════════════════════════════════════════════════════

function updatePositionsTable(positions, tableId) {
    const tbody = document.querySelector('#' + tableId + ' tbody');
    if (!tbody) return;
    tbody.innerHTML = positions.map(p => `
        <tr>
            <td>${p.stockcode || ''}</td>
            <td>${p.quantity || 0}</td>
            <td>${(p.avg_cost || 0).toFixed(2)}</td>
            <td>${(p.price || 0).toFixed(2)}</td>
            <td>${fmtWan(p.market_value || 0)}</td>
            <td style="color:${(p.unrealized_pnl||0)>=0?'#e74c3c':'#27ae60'}">${fmtWan(p.unrealized_pnl||0)}</td>
        </tr>
    `).join('');
}

function updateTradesTable(trades, tableId) {
    const tbody = document.querySelector('#' + tableId + ' tbody');
    if (!tbody) return;
    tbody.innerHTML = trades.slice(-30).reverse().map(o => `
        <tr>
            <td>${o.time || o.date || ''}</td>
            <td>${o.stockcode || ''}</td>
            <td style="color:${o.side==='BUY'?'#e74c3c':'#27ae60'}">${o.side==='BUY'?'买入':'卖出'}</td>
            <td>${o.quantity || 0}</td>
            <td>${(o.price || 0).toFixed(2)}</td>
        </tr>
    `).join('');
}

function updateNavChart(data, chart) {
    if (!chart || !data.length) return;
    chart.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: data.map(d => d.date || d.time || ''), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: v => v.toFixed(2) } },
        series: [{
            name: '净值', type: 'line',
            data: data.map(d => d.nav),
            smooth: true,
            lineStyle: { color: '#2980b9', width: 2 },
            areaStyle: { color: 'rgba(41,128,185,0.1)' },
        }],
    }, true);
}

function updateIntradayChart(data) {
    if (!chartIntraday || !data.length) return;
    chartIntraday.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: data.map(d => d.time || ''), axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value' },
        series: [{
            name: '总资产', type: 'line',
            data: data.map(d => d.total_value),
            smooth: true, lineStyle: { color: '#27ae60', width: 2 },
            areaStyle: { color: 'rgba(39,174,96,0.1)' },
        }, {
            name: '可用资金', type: 'line',
            data: data.map(d => d.capital),
            smooth: true, lineStyle: { color: '#e67e22', width: 1, type: 'dashed' },
        }],
    }, true);
}

// ── 工具 ─────────────────────────────────────────────

function fmtWan(val) {
    if (val == null || isNaN(val)) return '--';
    const num = Math.abs(val);
    if (num >= 1e8) return (val / 1e8).toFixed(2) + '亿';
    if (num >= 1e4) return (val / 1e4).toFixed(2) + '万';
    return val.toFixed(2);
}
