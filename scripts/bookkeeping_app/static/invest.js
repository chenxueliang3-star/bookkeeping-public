/* 投资理财页：三层结构展示 + 记账表单 + K线图 + 盈亏曲线。
   自包含模块：独立取数/渲染，通过 window.renderInvest 与主面板导航集成。 */
(() => {
  'use strict';

  let overview = null;
  let cashAccounts = [];
  let activeChart = null; // {canvas, mode:'kline'|'curve', data}
  let quoteStatus = '';
  let quoteRefreshing = false;

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  /* 常用币种快捷选择：选中自动填名称+代码；列表外的持仓（个股/基金/冷门币）仍可手动输入 */
  const COMMON_COINS = [
    {name: '比特币', code: 'BTC'}, {name: '以太坊', code: 'ETH'}, {name: '泰达币', code: 'USDT'},
    {name: '币安币', code: 'BNB'}, {name: 'Solana', code: 'SOL'}, {name: '瑞波币', code: 'XRP'},
    {name: '狗狗币', code: 'DOGE'}, {name: '艾达币', code: 'ADA'}, {name: '波场', code: 'TRX'},
    {name: 'Avalanche', code: 'AVAX'}, {name: 'Chainlink', code: 'LINK'}, {name: '波卡', code: 'DOT'},
  ];
  const money = (cents) => (Number(cents || 0) / 100).toFixed(2);
  const cny = (cents) => `¥${money(cents)}`;
  const quoteMoney = (cents, currency) => `${currency === 'USD' ? '$' : '¥'}${money(cents)}`;
  const fxValue = (micros) => (Number(micros || 1000000) / 1000000).toFixed(4);
  const signed = (cents) => {
    const value = Number(cents || 0) / 100;
    if (Math.abs(value) < 0.005) return '±0.00';
    return (value > 0 ? '+' : '-') + Math.abs(value).toFixed(2);
  };
  const signedCny = (cents) => `¥${signed(cents)}`;
  const toneClass = (cents) => Number(cents || 0) > 0 ? 'pos' : Number(cents || 0) < 0 ? 'neg' : '';
  const pct = (num, den) => (den && Number(den) !== 0) ? `(${(Number(num) / Number(den) * 100).toFixed(2)}%)` : '';
  const nowLocal = () => {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  async function api(path, options) {
    const resp = await fetch(path, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }
  const post = (path, payload) => api(path, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)});

  /* ------------------------------------------------------ 渲染 */

  function statCard(label, value, cls, sub) {
    return `<div class="inv-stat"><div class="inv-stat-label">${esc(label)}</div>
      <div class="inv-stat-value ${cls || ''}">${esc(value)}</div>
      ${sub ? `<div class="inv-stat-sub">${esc(sub)}</div>` : ''}</div>`;
  }

  function assetRowsHtml(account) {
    if (!account.assets.length) return '<tr><td colspan="8" class="inv-empty">暂无持仓，点右上"记一笔交易"或"添加持仓"</td></tr>';
    return account.assets.map(a => {
      const market = a.market_cents !== null && a.market_cents !== undefined ? cny(a.market_cents) : '—';
      const unreal = a.unrealized_cents;
      const unrealText = unreal === null || unreal === undefined ? '—' : `${signedCny(unreal)} ${pct(unreal, a.cost_cents)}`;
      const price = a.price_cents !== null && a.price_cents !== undefined
        ? `${quoteMoney(a.price_cents, a.price_currency)}${a.price_currency === 'USD' && a.price_cny_cents ? ` <span class="inv-cny-equivalent">≈ ${cny(a.price_cny_cents)}</span>` : ''}`
        : '—';
      const avg = a.avg_cost_cents !== null && a.avg_cost_cents !== undefined ? cny(a.avg_cost_cents) : (a.cost_cents ? '待填成本' : '—');
      return `<tr>
        <td><div class="inv-asset-name">${esc(a.name)}${a.code ? `<span class="inv-code">${esc(a.code)}</span>` : ''}</div></td>
        <td class="num">${a.quantity ? esc(String(a.quantity)) : '—'}</td>
        <td class="num">${esc(avg)}</td>
        <td class="num"><button class="inv-link" data-price-edit="${a.id}" data-price="${a.price_cents == null ? '' : money(a.price_cents)}" data-currency="${esc(a.price_currency || 'CNY')}" data-fx="${a.fx_cny_micros || 1000000}" title="更新现价">${price} ✎</button></td>
        <td class="num">${esc(market)}</td>
        <td class="num ${toneClass(unreal)}">${esc(unrealText)}</td>
        <td class="num ${toneClass(a.realized_cents)}">${a.realized_cents ? esc(signedCny(a.realized_cents)) : '—'}</td>
        <td class="inv-actions">
          <button class="inv-link" data-kline="${a.id}">K线</button>
          <button class="inv-link" data-curve="${a.id}">曲线</button>
          ${a.unsettled_cents ? `<button class="inv-link" data-asset-settle="${a.id}" data-amount="${a.unsettled_cents}" title="把这笔浮盈算进现金，数量不变，成本提到现价">兑现浮盈</button>` : ''}
          <button class="inv-link danger" data-asset-del="${a.id}">删</button>
        </td>
      </tr>`;
    }).join('');
  }

  function accountBlockHtml(account) {
    const unsettled = account.unsettled_cents || 0;
    return `<div class="inv-account">
      <div class="inv-account-head">
        <span class="inv-account-name">${esc(account.name)}</span>
        <span class="muted">净投入 ${esc(cny(account.cost_cents))}</span>
        <span class="muted">可兑现浮盈 <span class="${toneClass(unsettled)}">${esc(signedCny(unsettled))}</span></span>
        <button class="inv-link" data-account-settle="${account.id}" data-amount="${unsettled}" title="把这个账户里每笔持仓的浮盈分别算进现金，数量不变">全部兑现浮盈</button>
        <button class="inv-link danger" data-account-del="${account.id}">删除账户</button>
      </div>
      <table class="inv-table">
        <colgroup><col class="col-asset"><col class="col-qty"><col class="col-money"><col class="col-money"><col class="col-money"><col class="col-profit"><col class="col-profit"><col class="col-actions"></colgroup>
        <thead><tr><th>名称</th><th>数量</th><th>摊薄成本</th><th>现价</th><th>市值</th><th>浮盈</th><th>已实现</th><th></th></tr></thead>
        <tbody>${assetRowsHtml(account)}</tbody>
      </table>
    </div>`;
  }

  function render() {
    const root = $('investRoot');
    if (!root) return;
    if (!overview) {
      root.innerHTML = '<div class="muted">加载中…</div>';
      return;
    }
    const t = overview.totals || {};
    const unreal = t.unrealized_cents;
    const unrealText = unreal === null || unreal === undefined ? '—' : `${signedCny(unreal)} ${pct(unreal, t.cost_cents)}`;
    const cats = overview.categories || [];
    const catsHtml = cats.length ? cats.map(cat => {
      const accounts = cat.accounts || [];
      const body = accounts.length
        ? accounts.map(accountBlockHtml).join('')
        : '<div class="inv-empty">该大类下还没有账户</div>';
      return `<div class="inv-category">
        <div class="inv-category-head">
          <span class="inv-category-name">${esc(cat.name)}</span>
          <span class="muted">市值 ${cat.market_cents ? esc(cny(cat.market_cents)) : '—'} · 净投入 ${esc(cny(cat.cost_cents))} · 已实现 <span class="${toneClass(cat.realized_cents)}">${esc(signedCny(cat.realized_cents))}</span></span>
          <button class="inv-link" data-account-add="${cat.id}">+ 添加账户</button>
          ${accounts.length ? '' : `<button class="inv-link danger" data-category-del="${cat.id}">删大类</button>`}
        </div>
        ${body}
        <div class="inv-add-asset-row"><button class="inv-link" data-asset-add="${cat.id}">+ 添加持仓</button></div>
      </div>`;
    }).join('') : '<div class="inv-empty">还没有大类，点右上"添加大类"开始</div>';

    root.innerHTML = `
      <div class="inv-topbar">
        <div class="inv-stats">
          ${statCard('总市值（人民币）', cny(t.market_cents))}
          ${statCard('总净投入（人民币）', cny(t.cost_cents))}
          ${statCard('浮盈', unrealText, toneClass(unreal))}
          ${statCard('可兑现浮盈（人民币）', signedCny(t.unsettled_cents || 0), toneClass(t.unsettled_cents))}
          ${statCard('本月已实现（人民币）', signedCny(t.realized_month_cents), toneClass(t.realized_month_cents))}
          ${statCard('累计已实现（人民币）', signedCny(t.realized_total_cents), toneClass(t.realized_total_cents))}
        </div>
        <div class="inv-toolbar">
          <button class="inv-btn quote-refresh" data-quote-refresh ${quoteRefreshing ? 'disabled' : ''}>${quoteRefreshing ? '刷新中…' : '刷新行情'}</button>
          ${quoteStatus ? `<span class="inv-quote-status">${esc(quoteStatus)}</span>` : ''}
          <button class="inv-btn primary" data-trade-add>记一笔交易</button>
          <button class="inv-btn" data-asset-add-global>添加持仓</button>
          <button class="inv-btn" data-category-add>添加大类</button>
        </div>
      </div>
      <div id="invFormPanel"></div>
      <div id="invChartPanel" class="inv-chart-panel hidden"></div>
      ${catsHtml}`;

    bindActions();
  }

  /* ------------------------------------------------------ 表单 */

  function formHtml(kind, preset) {
    const cashOptions = cashAccounts.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
    const catOptions = (overview.categories || []).map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    if (kind === 'category') {
      return `<div class="inv-form"><h4>添加大类</h4>
        <label>名称<input id="invf-name" placeholder="如：黄金 / 外汇"></label>
        <div class="inv-form-actions"><button class="inv-btn primary" data-form-save="category">保存</button><button class="inv-btn" data-form-cancel>取消</button></div></div>`;
    }
    if (kind === 'account') {
      return `<div class="inv-form"><h4>添加账户（${esc(preset.categoryName)}）</h4>
        <label>账户名<input id="invf-name" placeholder="如：币安 / 华泰证券 / 支付宝基金"></label>
        <label>备注<input id="invf-note" placeholder="选填"></label>
        <div class="inv-form-actions"><button class="inv-btn primary" data-form-save="account" data-cat="${preset.categoryId}">保存</button><button class="inv-btn" data-form-cancel>取消</button></div></div>`;
    }
    if (kind === 'asset') {
      return `<div class="inv-form"><h4>添加持仓</h4>
        <label>所属账户<select id="invf-account">${accountOptionsHtml(preset.categoryId, preset.accountId)}</select></label>
        <label>选择币种（快捷填充，可搜索）<input id="invf-coin" list="invf-coin-list" placeholder="如：比特币 / BTC（选填）">
          <datalist id="invf-coin-list">${COMMON_COINS.map(c => `<option value="${esc(c.name)}（${esc(c.code)}）">`).join('')}</datalist></label>
        <label>名称<input id="invf-name" placeholder="如：比特币 / 贵州茅台 / 沪深300ETF"></label>
        <label>代码<input id="invf-code" placeholder="行情用：BTC / 600519（选填）"></label>
        <div class="inv-form-row">
          <label>当前持有数量<input id="invf-qty" inputmode="decimal" placeholder="0"></label>
          <label>建仓摊薄单价<input id="invf-price" inputmode="decimal" placeholder="选填"><select id="invf-currency"><option value="CNY">人民币 CNY</option><option value="USD">美元 USD</option></select></label>
          <label id="invf-fx-wrap" class="hidden">美元兑人民币汇率<input id="invf-fx" inputmode="decimal" value="7.2000"></label>
        </div>
        <label>资金账户（有数量时自动生成一笔买入）<select id="invf-cash">${cashOptions}</select></label>
        <div class="inv-hint">从"选择币种"挑选会自动填好名称和代码；列表没有的（个股/基金/冷门币）直接手动输入即可。填了数量和单价会自动生成一笔"买入"作为建仓成本。</div>
        <div class="inv-form-actions"><button class="inv-btn primary" data-form-save="asset">保存</button><button class="inv-btn" data-form-cancel>取消</button></div></div>`;
    }
    if (kind === 'trade') {
      tradeAssetIndex = [];
      const scopeSet = new Set();
      (overview.categories || []).forEach(c => {
        scopeSet.add(c.name);
        (c.accounts || []).forEach(a => {
          scopeSet.add(a.name);
          (a.assets || []).forEach(s => tradeAssetIndex.push({id: s.id, name: s.name, cat: c.name, acc: a.name}));
        });
      });
      tradeScopeSet = scopeSet;
      return `<div class="inv-form"><h4>记一笔交易</h4>
        <label>类型<select id="invf-type"><option>买入</option><option>卖出</option><option>分红</option><option>利息</option><option>手续费</option></select></label>
        <label>名称</label>
        <div class="inv-form-row">
          <select id="invf-asset-scope">
            <option value="">全部（大类/账户）</option>
            ${[...scopeSet].map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')}
          </select>
          <input id="invf-asset-name" list="invf-name-list" placeholder="搜索：币种/个股" autocomplete="off">
          <datalist id="invf-name-list"></datalist>
        </div>
        <div class="inv-form-row">
          <label>数量<input id="invf-qty" inputmode="decimal" placeholder="买入/卖出必填"></label>
          <label>单价<input id="invf-price" inputmode="decimal" placeholder="买入/卖出必填"><span id="invf-price-unit" class="muted">根据持仓币种</span></label>
        </div>
        <label id="invf-trade-fx-wrap" class="hidden">美元兑人民币汇率<input id="invf-trade-fx" inputmode="decimal" value="7.2000"></label>
        <div class="inv-form-row">
          <label>金额（元，分红/利息/手续费用）<input id="invf-amount" inputmode="decimal" placeholder="选填"></label>
          <label>手续费（元）<input id="invf-fee" inputmode="decimal" placeholder="0"></label>
        </div>
        <label>资金账户<select id="invf-cash">${cashOptions}</select></label>
        <label>时间<input id="invf-time" value="${esc(nowLocal())}"></label>
        <label>备注<input id="invf-note" placeholder="选填"></label>
        <div class="inv-hint">左边下拉选择大类/账户（可不选），右边搜索币种/个股；只匹配已添加的持仓，搜不到就先点"添加持仓"。买入=转账（现金→持仓），卖出=转账（持仓→现金），均不计收支；分红/利息计收入，手续费计支出。</div>
        <div class="inv-form-actions">
          <button class="inv-btn primary" data-form-save="trade">保存</button>
          <button class="inv-btn" data-goto-asset>还没添加？切换到「添加持仓」</button>
          <button class="inv-btn" data-form-cancel>取消</button>
        </div></div>`;
    }
    return '';
  }

  let tradeAssetIndex = [];
  let tradeScopeSet = new Set();

  function refreshTradeNameList() {
    const panel = $('invFormPanel');
    if (!panel) return;
    const scopeInput = panel.querySelector('#invf-asset-scope');
    const list = panel.querySelector('#invf-name-list');
    if (!scopeInput || !list) return;
    const scope = scopeInput.value.trim();
    const names = new Set();
    tradeAssetIndex.forEach(a => {
      if (!scope || a.cat === scope || a.acc === scope) names.add(a.name);
    });
    list.innerHTML = [...names].map(n => `<option value="${esc(n)}">`).join('');
    // 已输入的名称若不在过滤结果里则清空，避免保存时误报
    const nameInput = panel.querySelector('#invf-asset-name');
    if (nameInput && nameInput.value && names.size && !names.has(nameInput.value)) {
      nameInput.value = '';
    }
  }

  function accountOptionsHtml(categoryId, accountId) {
    const cats = overview.categories || [];
    let cat = cats.find(c => String(c.id) === String(categoryId));
    if (accountId) {
      const owner = cats.find(c => (c.accounts || []).some(a => String(a.id) === String(accountId)));
      if (owner) cat = owner;
    }
    if (!cat) cat = cats[0];
    if (!cat || !(cat.accounts || []).length) return '<option value="">该大类下无账户，先添加账户</option>';
    return cat.accounts.map(a => `<option value="${a.id}"${String(a.id) === String(accountId) ? ' selected' : ''}>${esc(a.name)}</option>`).join('');
  }

  /* ------------------------------------------------------ 图表 */

  const UP = '#E24B4A';   // 涨：红
  const DOWN = '#1D9E75'; // 跌：绿

  function showChartPanel(title, subtitle) {
    const panel = $('invChartPanel');
    panel.classList.remove('hidden');
    panel.innerHTML = `<div class="inv-chart-head">
        <div><strong>${esc(title)}</strong><span class="muted"> ${esc(subtitle || '')}</span></div>
        <div class="inv-chart-tools">
          <button class="inv-link" data-kint="day">日</button>
          <button class="inv-link" data-kint="week">周</button>
          <button class="inv-link" data-kint="month">月</button>
          <button class="inv-link danger" data-chart-close>关闭</button>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="invChart" width="900" height="320"></canvas></div>`;
    panel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }

  function drawKline(klines, currency) {
    const canvas = $('invChart');
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.width = 900 * dpr, H = canvas.height = 320 * dpr;
    canvas.style.width = '100%';
    ctx.scale(dpr, dpr);
    const w = 900, h = 320, padL = 8, padR = 64, padT = 10, padB = 24;
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue('--ink').trim() || '#E8E6E0';
    const grid = 'rgba(128,128,128,.18)';
    if (!klines.length) {
      ctx.fillStyle = ink; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('暂无行情数据（可填代码后重试，或手动更新现价积累净值点）', w / 2, h / 2);
      return;
    }
    let hi = -Infinity, lo = Infinity;
    klines.forEach(k => { hi = Math.max(hi, k.high); lo = Math.min(lo, k.low); });
    const range = hi - lo || 1;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const step = plotW / klines.length;
    const bw = Math.max(Math.min(step * 0.62, 14), 1.5);
    const y = (v) => padT + (1 - (v - lo) / range) * plotH;

    ctx.strokeStyle = grid; ctx.fillStyle = ink; ctx.font = '11px sans-serif'; ctx.textAlign = 'left';
    for (let i = 0; i <= 4; i++) {
      const v = lo + range * i / 4;
      const yy = y(v);
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.fillText(`${currency === 'USD' ? '$' : '¥'}${(v / 100).toFixed(2)}`, padL + plotW + 6, yy + 3);
    }
    klines.forEach((k, i) => {
      const cx = padL + step * i + step / 2;
      const up = k.close >= k.open;
      const color = up ? UP : DOWN;
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, y(k.high)); ctx.lineTo(cx, y(k.low)); ctx.stroke();
      const top = y(Math.max(k.open, k.close)), bot = y(Math.min(k.open, k.close));
      ctx.fillRect(cx - bw / 2, top, bw, Math.max(bot - top, 1));
      if (i % Math.ceil(klines.length / 8) === 0) {
        ctx.fillStyle = 'rgba(128,128,128,.8)'; ctx.textAlign = 'center';
        ctx.fillText(k.date.slice(5), cx, h - 6);
        ctx.fillStyle = color; ctx.textAlign = 'left';
      }
    });
  }

  function drawCurve(points) {
    const canvas = $('invChart');
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = 900 * dpr; canvas.height = 320 * dpr;
    canvas.style.width = '100%';
    ctx.scale(dpr, dpr);
    const w = 900, h = 320, padL = 8, padR = 64, padT = 10, padB = 24;
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue('--ink').trim() || '#E8E6E0';
    const grid = 'rgba(128,128,128,.18)';
    if (!points || points.length < 2) {
      ctx.fillStyle = ink; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('数据点不足，先记几笔交易或更新现价', w / 2, h / 2);
      return;
    }
    const values = points.flatMap(p => [p.market_cents, p.cost_cents]);
    let hi = Math.max(...values), lo = Math.min(...values);
    if (hi === lo) { hi += 100; lo -= 100; }
    const range = hi - lo;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const x = (i) => padL + i / (points.length - 1) * plotW;
    const y = (v) => padT + (1 - (v - lo) / range) * plotH;

    ctx.strokeStyle = grid; ctx.font = '11px sans-serif'; ctx.textAlign = 'left';
    for (let i = 0; i <= 4; i++) {
      const v = lo + range * i / 4;
      ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(padL + plotW, y(v)); ctx.stroke();
      ctx.fillText((v / 100).toFixed(2), padL + plotW + 6, y(v) + 3);
    }
    const line = (key, color, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.setLineDash(dash || []);
      ctx.beginPath();
      points.forEach((p, i) => i ? ctx.lineTo(x(i), y(p[key])) : ctx.moveTo(x(i), y(p[key])));
      ctx.stroke(); ctx.setLineDash([]);
    };
    line('cost_cents', 'rgba(136,135,128,.9)', [4, 3]);
    line('market_cents', '#1D9E75');
    ctx.fillStyle = 'rgba(128,128,128,.8)'; ctx.textAlign = 'center';
    [0, Math.floor(points.length / 2), points.length - 1].forEach(i => ctx.fillText(points[i].date.slice(5), x(i), h - 6));
    ctx.fillStyle = '#1D9E75'; ctx.textAlign = 'left';
    ctx.fillText('— 市值', padL + 6, padT + 12);
    ctx.fillStyle = 'rgba(136,135,128,.9)';
    ctx.fillText('-- 净投入成本', padL + 62, padT + 12);
  }

  async function openKline(assetId, interval) {
    const asset = findAsset(assetId);
    if (!asset) return;
    showChartPanel(`${asset.name} K线`, '红涨绿跌 · 数据仅供展示，盈亏以你的交易记录为准');
    try {
      const data = await api(`/api/invest/kline?asset_id=${assetId}&interval=${interval || 'day'}`);
      drawKline(data.klines || [], data.currency || asset.price_currency || 'CNY');
      const sub = document.querySelector('#invChartPanel .inv-chart-head .muted');
      if (sub) sub.textContent = ` ${data.currency || asset.price_currency || 'CNY'} · 来源：${data.source === 'manual' ? '手动价格（净值近似）' : data.source}`;
    } catch (err) {
      const canvas = $('invChart');
      if (canvas) {
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#E8E6E0'; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
        ctx.fillText(`行情获取失败：${err.message}`, 450, 160);
      }
    }
  }

  async function openCurve(scope, id, title) {
    showChartPanel(title, '市值 vs 净投入成本');
    try {
      const data = await api(`/api/invest/curve?scope=${scope}&id=${id}`);
      drawCurve(data.points || []);
    } catch (err) {
      const canvas = $('invChart');
      if (canvas) {
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#E8E6E0'; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
        ctx.fillText(`加载失败：${err.message}`, 450, 160);
      }
    }
  }

  function findAsset(assetId) {
    for (const c of (overview.categories || [])) for (const a of (c.accounts || [])) for (const s of (a.assets || []))
      if (String(s.id) === String(assetId)) return s;
    return null;
  }
  function findAccount(accountId) {
    for (const c of (overview.categories || [])) for (const a of (c.accounts || []))
      if (String(a.id) === String(accountId)) return {account: a, category: c};
    return null;
  }

  /* ------------------------------------------------------ 事件 */

  function bindActions() {
    const root = $('investRoot');
    if (!root) return;
    root.querySelectorAll('[data-quote-refresh]').forEach(b => b.onclick = () => refreshPrices(true));

    root.querySelectorAll('[data-trade-add]').forEach(b => b.onclick = () => { $('invFormPanel').innerHTML = formHtml('trade'); bindForm('trade'); });
    root.querySelectorAll('[data-category-add]').forEach(b => b.onclick = () => { $('invFormPanel').innerHTML = formHtml('category'); bindForm('category'); });
    root.querySelectorAll('[data-account-add]').forEach(b => b.onclick = () => {
      const cat = (overview.categories || []).find(c => String(c.id) === b.dataset.accountAdd);
      $('invFormPanel').innerHTML = formHtml('account', {categoryId: cat.id, categoryName: cat.name}); bindForm('account', {categoryId: cat.id});
    });
    root.querySelectorAll('[data-asset-add]').forEach(b => b.onclick = () => {
      const cat = (overview.categories || []).find(c => String(c.id) === b.dataset.assetAdd);
      $('invFormPanel').innerHTML = formHtml('asset', {categoryId: cat.id}); bindForm('asset', {categoryId: cat.id});
    });
    root.querySelectorAll('[data-asset-add-global]').forEach(b => b.onclick = () => {
      const cats = overview.categories || [];
      const target = cats.find(c => (c.accounts || []).length) || cats[0];
      if (!target || !(target.accounts || []).length) {
        alert('还没有可用的账户。请先在下方大类标题行点「添加账户」，再添加持仓。');
        return;
      }
      $('invFormPanel').innerHTML = formHtml('asset', {categoryId: target.id}); bindForm('asset', {categoryId: target.id});
    });
    root.querySelectorAll('[data-category-del]').forEach(b => b.onclick = async () => {
      if (!confirm('删除该大类？')) return;
      try { await post('/api/invest/category-delete', {id: Number(b.dataset.categoryDel)}); await load(); }
      catch (err) { alert(err.message); }
    });
    root.querySelectorAll('[data-account-del]').forEach(b => b.onclick = async () => {
      if (!confirm('删除该账户？')) return;
      try { await post('/api/invest/account-delete', {id: Number(b.dataset.accountDel)}); await load(); }
      catch (err) { alert(err.message); }
    });
    root.querySelectorAll('[data-asset-settle]').forEach(b => b.onclick = async () => {
      const amount = Number(b.dataset.amount || 0) / 100;
      const label = amount > 0 ? `盈利 ¥${amount.toFixed(2)} 将记为「收入/投资收益」` : `亏损 ¥${Math.abs(amount).toFixed(2)} 将记为「支出/投资亏损」`;
      if (!confirm(`确认兑现这笔浮盈？\n${label}\n数量不变，成本提到现价，涨出来的钱算进理财账户。`)) return;
      try { await post('/api/invest/asset-settle', {id: Number(b.dataset.assetSettle)}); await load(); }
      catch (err) { alert(err.message); }
    });
    root.querySelectorAll('[data-account-settle]').forEach(b => b.onclick = async () => {
      const amount = Number(b.dataset.amount || 0) / 100;
      const label = amount > 0 ? `盈利 ¥${amount.toFixed(2)} 将记为「收入/投资收益」`
        : amount < 0 ? `浮亏 ¥${Math.abs(amount).toFixed(2)} 将记为「支出/投资亏损」` : '当前没有可兑现浮盈';
      if (!confirm(`确认把这个账户里的持仓全部兑现浮盈？\n${label}\n每笔持仓数量不变，成本提到现价，钱算进理财账户。`)) return;
      try { await post('/api/invest/account-settle', {id: Number(b.dataset.accountSettle)}); await load(); }
      catch (err) { alert(err.message); }
    });
    root.querySelectorAll('[data-asset-del]').forEach(b => b.onclick = async () => {
      if (!confirm('删除这个持仓及其全部交易记录？（账本分录会同步回滚）')) return;
      try { await post('/api/invest/asset-delete', {id: Number(b.dataset.assetDel)}); await load(); }
      catch (err) { alert(err.message); }
    });
    root.querySelectorAll('[data-price-edit]').forEach(b => b.onclick = async () => {
      const currency = prompt('现价币种：CNY 或 USD', b.dataset.currency || 'CNY');
      if (currency === null) return;
      const normalized = currency.trim().toUpperCase();
      if (!['CNY', 'USD'].includes(normalized)) return alert('请输入 CNY 或 USD');
      const input = prompt(`更新现价（${normalized}）：`, b.dataset.price || '');
      if (input === null) return;
      const cents = Math.round(parseFloat(input) * 100);
      if (!cents || cents <= 0) return alert('请输入有效价格');
      const fx = normalized === 'USD' ? parseFloat(prompt('美元兑人民币汇率：', fxValue(b.dataset.fx))) : 1;
      if (!fx || fx <= 0) return alert('请输入有效汇率');
      try { await post('/api/invest/asset-price', {id: Number(b.dataset.priceEdit), price_cents: cents, quote_currency: normalized, fx_cny_micros: Math.round(fx * 1000000)}); await load(); }
      catch (err) { alert(err.message); }
    });
    root.querySelectorAll('[data-kline]').forEach(b => b.onclick = () => {
      activeChart = {mode: 'kline', assetId: b.dataset.kline};
      openKline(b.dataset.kline, 'day');
    });
    root.querySelectorAll('[data-curve]').forEach(b => b.onclick = () => {
      activeChart = {mode: 'curve'};
      const asset = findAsset(b.dataset.curve);
      openCurve('asset', b.dataset.curve, `${asset ? asset.name : ''} 市值曲线`);
    });

    const panel = $('invChartPanel');
    if (panel) {
      panel.querySelectorAll('[data-chart-close]').forEach(b => b.onclick = () => panel.classList.add('hidden'));
      panel.querySelectorAll('[data-kint]').forEach(b => b.onclick = () => {
        if (activeChart && activeChart.mode === 'kline') openKline(activeChart.assetId, b.dataset.kint);
      });
    }
  }

  function bindForm(kind, preset = {}) {
    const panel = $('invFormPanel');
    panel.querySelector('[data-form-cancel]').onclick = () => { panel.innerHTML = ''; };
    if (kind === 'asset') {
      const currencyEl = panel.querySelector('#invf-currency');
      const fxWrap = panel.querySelector('#invf-fx-wrap');
      const syncAssetCurrency = () => fxWrap.classList.toggle('hidden', currencyEl.value !== 'USD');
      currencyEl.addEventListener('change', syncAssetCurrency);
      syncAssetCurrency();
      const coinInput = panel.querySelector('#invf-coin');
      coinInput.addEventListener('input', () => {
        const v = coinInput.value.trim();
        const hit = COMMON_COINS.find(c => v === `${c.name}（${c.code}）` || c.code.toUpperCase() === v.toUpperCase() || c.name === v);
        if (hit) {
          panel.querySelector('#invf-name').value = hit.name;
          panel.querySelector('#invf-code').value = hit.code;
        }
      });
      if (preset.coin) {
        coinInput.value = preset.coin;
        coinInput.dispatchEvent(new Event('input'));
        const nameEl = panel.querySelector('#invf-name');
        if (nameEl && !nameEl.value) nameEl.value = preset.coin;  // 非内置币种（个股/基金等）把输入直接落到名称
      }
    }
    if (kind === 'trade') {
      panel.querySelector('#invf-asset-scope').addEventListener('change', refreshTradeNameList);
      refreshTradeNameList();
      const gotoAsset = panel.querySelector('[data-goto-asset]');
      if (gotoAsset) gotoAsset.onclick = () => {
        const scope = panel.querySelector('#invf-asset-scope').value.trim();
        const typed = panel.querySelector('#invf-asset-name').value.trim();
        let categoryId = null, accountId = null;
        for (const c of (overview.categories || [])) {
          if (c.name === scope) { categoryId = c.id; break; }
          const hitAcc = (c.accounts || []).find(a => a.name === scope);
          if (hitAcc) { categoryId = c.id; accountId = hitAcc.id; break; }
        }
        if (categoryId === null) {
          const cats = overview.categories || [];
          const target = cats.find(c => (c.accounts || []).length) || cats[0];
          if (!target || !(target.accounts || []).length) { alert('还没有可用的账户。请先在下方大类标题行点「添加账户」，再添加持仓。'); return; }
          categoryId = target.id;
        }
        const preset = {categoryId, accountId, coin: typed};
        $('invFormPanel').innerHTML = formHtml('asset', preset);
        bindForm('asset', preset);
      };
    }
    panel.querySelector('[data-form-save]').onclick = async (event) => {
      const btn = event.currentTarget;
      btn.disabled = true;
      try {
        if (kind === 'category') await post('/api/invest/category-add', {name: panel.querySelector('#invf-name').value.trim()});
        else if (kind === 'account') await post('/api/invest/account-add', {category_id: Number(preset.categoryId), name: panel.querySelector('#invf-name').value.trim(), note: panel.querySelector('#invf-note').value.trim()});
        else if (kind === 'asset') {
          const accountId = Number(panel.querySelector('#invf-account').value);
          const name = panel.querySelector('#invf-name').value.trim();
          if (!accountId) throw new Error('该大类下无账户，先添加账户');
          const quoteCurrency = panel.querySelector('#invf-currency').value;
          const fx = quoteCurrency === 'USD' ? parseFloat(panel.querySelector('#invf-fx').value) : 1;
          await post('/api/invest/asset-add', {account_id: accountId, name, code: panel.querySelector('#invf-code').value.trim(), quantity: panel.querySelector('#invf-qty').value.trim() || '0', quote_currency: quoteCurrency, fx_cny_micros: Math.round(fx * 1000000)});
          const price = parseFloat(panel.querySelector('#invf-price').value);
          const data = await api('/api/invest/overview');
          const newAsset = (() => { for (const c of data.categories) for (const a of c.accounts) for (const s of a.assets) if (s.name === name) return s; return null; })();
          if (newAsset && price > 0) {
            // 初始建仓：以数量×单价生成一笔买入（若有数量），并设现价
            const qty = parseFloat(panel.querySelector('#invf-qty').value || '0');
            if (qty > 0) {
              const cashAccount = panel.querySelector('#invf-cash') ? panel.querySelector('#invf-cash').value : cashOptionsFirst(cashAccounts);
              await post('/api/invest/trade-add', {
                asset_id: newAsset.id, trade_type: '买入', quantity: String(qty),
                price_cents: Math.round(price * 100), price_currency: quoteCurrency, fx_cny_micros: Math.round(fx * 1000000), fee_cents: 0,
                cash_account: cashAccount, trade_time: nowLocal(), note: '建仓成本（添加持仓时录入）',
              });
            } else {
              await post('/api/invest/asset-price', {id: newAsset.id, price_cents: Math.round(price * 100), quote_currency: quoteCurrency, fx_cny_micros: Math.round(fx * 1000000)});
            }
          }
        }
        else if (kind === 'trade') {
          const scope = panel.querySelector('#invf-asset-scope').value.trim();
          const assetName = panel.querySelector('#invf-asset-name').value.trim();
          if (!assetName) throw new Error('请填写币种/个股');
          const candidates = tradeAssetIndex.filter(a =>
            a.name === assetName && (!scope || a.cat === scope || a.acc === scope));
          if (!candidates.length) {
            const anyMatch = tradeAssetIndex.some(a => a.name === assetName);
            throw new Error(anyMatch
              ? `“${assetName}”不在“${scope}”下，请检查左边的大类/账户，或先添加持仓`
              : `未找到持仓“${assetName}”。\n如需建仓买入，请点表单下方「还没添加？切换到“添加持仓”」按钮，先建档再记账。`);
          }
          if (candidates.length > 1) throw new Error(`“${assetName}”在多个账户下存在，请在左边选择大类/账户区分`);
          const payload = {
            asset_id: candidates[0].id,
            trade_type: panel.querySelector('#invf-type').value,
            quantity: panel.querySelector('#invf-qty').value.trim() || null,
            price_cents: Math.round(parseFloat(panel.querySelector('#invf-price').value || '0') * 100) || null,
            price_currency: candidates[0].price_currency || 'CNY',
            fx_cny_micros: candidates[0].fx_cny_micros || 1000000,
            amount_cents: Math.round(parseFloat(panel.querySelector('#invf-amount').value || '0') * 100) || null,
            fee_cents: Math.round(parseFloat(panel.querySelector('#invf-fee').value || '0') * 100) || 0,
            cash_account: panel.querySelector('#invf-cash').value,
            trade_time: panel.querySelector('#invf-time').value.trim(),
            note: panel.querySelector('#invf-note').value.trim(),
          };
          await post('/api/invest/trade-add', payload);
        }
        panel.innerHTML = '';
        await load();
      } catch (err) {
        alert(err.message);
        btn.disabled = false;
      }
    };
  }

  const cashOptionsFirst = (list) => list[0] || '';

  /* ------------------------------------------------------ 加载 */

  async function refreshPrices(showErrors) {
    if (quoteRefreshing) return;
    quoteRefreshing = true;
    quoteStatus = '正在获取最新报价…';
    render();
    try {
      const result = await post('/api/invest/refresh-prices', {});
      quoteStatus = result.updated
        ? `已更新 ${result.updated} 个持仓 · ${new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`
        : '暂无可用实时行情，已保留原价格';
      overview = await api('/api/invest/overview');
      render();
    } catch (err) {
      quoteStatus = '行情获取失败，已保留原价格';
      render();
      if (showErrors) alert(err.message);
    } finally {
      quoteRefreshing = false;
      render();
    }
  }

  async function load() {
    try {
      const [ov, accounts] = await Promise.all([api('/api/invest/overview'), api('/api/account-names')]);
      overview = ov;
      cashAccounts = (accounts || []).filter(name => !String(name).startsWith('理财·') && !String(name).startsWith('借款-') && !String(name).startsWith('借出-'));
      render();
      refreshPrices(false);
    } catch (err) {
      const root = $('investRoot');
      if (root) root.innerHTML = `<div class="inv-empty">加载失败：${esc(err.message)}</div>`;
    }
  }

  window.renderInvest = () => { render(); if (!overview) load(); };
  window.addEventListener('DOMContentLoaded', load);
  window.setInterval(() => {
    if (overview && document.visibilityState !== 'hidden') refreshPrices(false);
  }, 60000);
})();
