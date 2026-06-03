/**
 * 实盘模拟监控 — 前端
 */
const API = '/api/status';
let navChart, intradayChart;

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    updateClock();
    setInterval(updateClock, 1000);
    fetchData();
    setInterval(fetchData, 5000); // 每5秒刷新
    window.addEventListener('resize', () => { navChart?.resize(); intradayChart?.resize(); });
});

function initCharts() {
    navChart = echarts.init(document.getElementById('chart-nav'));
    intradayChart = echarts.init(document.getElementById('chart-intraday'));
}

function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleString('zh-CN');
    const h = now.getHours(), m = now.getMinutes();
    const inSession = (h === 9 && m >= 30 || h === 10 || h === 11 && m <= 30) ||
                      (h >= 13 && h < 15);
    const el = document.getElementById('market-status');
    if (inSession) { el.textContent = '交易中'; el.className = 'badge open'; }
    else { el.textContent = '休市'; el.className = 'badge'; }
}

async function fetchData() {
    try {
        const resp = await fetch(API);
        const json = await resp.json();
        if (json.account) updateDashboard(json);
    } catch (e) { console.log('等待数据...'); }
}

function updateDashboard(data) {
    const acc = data.account;
    document.getElementById('total-value').textContent = (acc.total_value / 1e4).toFixed(2) + '万';
    document.getElementById('capital').textContent = (acc.capital / 1e4).toFixed(2) + '万';
    const retEl = document.getElementById('total-return');
    retEl.textContent = (acc.total_return * 100).toFixed(2) + '%';
    retEl.className = 'card-value ' + (acc.total_return >= 0 ? 'positive' : 'negative');
    document.getElementById('pos-count').textContent = acc.position_count;

    updatePositions(data.positions || []);
    updateTrades(data.orders || []);
    updateNavChart(data.pnl_curve || []);
    updateIntradayChart(data.intraday || []);
}

function updatePositions(positions) {
    const tbody = document.querySelector('#positions-table tbody');
    tbody.innerHTML = positions.map(p => `
        <tr><td>${p.stockcode}</td><td>${p.quantity}</td><td>${p.avg_cost?.toFixed(2)}</td>
        <td>${p.price?.toFixed(2)}</td><td>${(p.market_value/1e4)?.toFixed(2)}万</td>
        <td style="color:${p.unrealized_pnl>=0?'#e74c3c':'#27ae60'}">${(p.unrealized_pnl/1e4)?.toFixed(2)}万</td></tr>
    `).join('');
}

function updateTrades(orders) {
    const tbody = document.querySelector('#trades-table tbody');
    tbody.innerHTML = orders.slice(-20).reverse().map(o => `
        <tr><td>${o.time}</td><td>${o.stockcode}</td>
        <td style="color:${o.side==='BUY'?'#e74c3c':'#27ae60'}">${o.side==='BUY'?'买入':'卖出'}</td>
        <td>${o.quantity}</td><td>${o.price?.toFixed(2)}</td></tr>
    `).join('');
}

function updateNavChart(data) {
    if (!data.length) return;
    navChart.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: data.map(d => d.date) },
        yAxis: { type: 'value', axisLabel: { formatter: v => v.toFixed(2) } },
        series: [{
            name: '净值', type: 'line', data: data.map(d => d.nav),
            smooth: true, lineStyle: { color: '#2980b9', width: 2 },
            areaStyle: { color: 'rgba(41,128,185,0.1)' }
        }]
    });
}

function updateIntradayChart(data) {
    if (!data.length) return;
    intradayChart.setOption({
        tooltip: { trigger: 'axis' },
        xAxis: { type: 'category', data: data.map(d => d.time) },
        yAxis: { type: 'value' },
        series: [{
            name: '总资产', type: 'line', data: data.map(d => d.total_value),
            smooth: true, lineStyle: { color: '#27ae60', width: 2 },
            areaStyle: { color: 'rgba(39,174,96,0.1)' }
        }, {
            name: '可用资金', type: 'line', data: data.map(d => d.capital),
            smooth: true, lineStyle: { color: '#e67e22', width: 1, type: 'dashed' }
        }]
    });
}
