const WIB_OFFSET_SEC = 7 * 3600;
const toUnixTs = (t) => {
  if (typeof t === 'number') return t;
  if (t && typeof t === 'object') {
    if (typeof t.timestamp === 'number') return t.timestamp;
    if (Number.isFinite(t.year) && Number.isFinite(t.month) && Number.isFinite(t.day)) {
      return Date.UTC(t.year, t.month - 1, t.day) / 1000;
    }
  }
  return 0;
};
const fmtWibHHMM = (t) => {
  const ts = toUnixTs(t) + WIB_OFFSET_SEC;
  const d = new Date(ts * 1000);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
};
const mkC = (el, h, extra = {}) => LightweightCharts.createChart(el, {
  width: el.clientWidth||900, height: h||el.clientHeight||400,
  layout: { background:{color:'rgba(0,0,0,0)'}, textColor:'#444444' },
  grid: { vertLines:{color:'#111111'}, horzLines:{color:'#111111'} },
  localization: {
    timeFormatter: (ts) => fmtWibHHMM(ts),
  },
  timeScale: {
    timeVisible:true,
    secondsVisible:false,
    borderColor:'#1a1a1a',
    tickMarkFormatter: (ts) => fmtWibHHMM(ts),
  },
  rightPriceScale: { borderColor:'#1a1a1a' },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  ...extra,
});

const btcEl = document.getElementById('chart-btc');
const rsiEl = document.getElementById('chart-rsi');
const BC = mkC(btcEl), RC = mkC(rsiEl, 68, { handleScroll:false, handleScale:false });

const CS  = BC.addCandlestickSeries({upColor:'#cccccc',downColor:'#333333',borderUpColor:'#cccccc',borderDownColor:'#444444',wickUpColor:'#888888',wickDownColor:'#444444'});
const PTB = BC.addLineSeries({color:'#555555',lineWidth:1,lineStyle:2,title:'PTB'});
const E9  = BC.addLineSeries({color:'#8c8c8c',lineWidth:2,title:'EMA9',priceLineVisible:false,lastValueVisible:false});
const E21 = BC.addLineSeries({color:'#727272',lineWidth:2,title:'EMA21',priceLineVisible:false,lastValueVisible:false});
const BBU = BC.addLineSeries({color:'rgba(132,132,132,.56)',lineWidth:1,lineStyle:0,priceLineVisible:false,lastValueVisible:false});
const BBM = BC.addLineSeries({color:'rgba(110,110,110,.52)',lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false});
const BBL = BC.addLineSeries({color:'rgba(132,132,132,.56)',lineWidth:1,lineStyle:0,priceLineVisible:false,lastValueVisible:false});
const RSI = RC.addLineSeries({color:'#888888',lineWidth:1,title:'RSI7'});
const ROB = RC.addLineSeries({color:'rgba(80,80,80,.4)',lineWidth:1,lineStyle:2});
const ROS = RC.addLineSeries({color:'rgba(60,60,60,.4)',lineWidth:1,lineStyle:2});

new ResizeObserver(()=>{
  BC.applyOptions({width:btcEl.clientWidth,height:btcEl.clientHeight});
  RC.applyOptions({width:rsiEl.clientWidth,height:rsiEl.clientHeight});
}).observe(btcEl);

const rsiResizeObserver = new ResizeObserver(()=>{
  RC.applyOptions({width:rsiEl.clientWidth,height:rsiEl.clientHeight});
});
rsiResizeObserver.observe(rsiEl);

function initChartSplitter(){
  const splitEl = document.getElementById('chart-splitter');
  const wrapEl = document.getElementById('chart-wrap');
  if(!splitEl || !wrapEl || !btcEl || !rsiEl) return;
  let dragging = false;
  let startY = 0;
  let startBtcH = 0;
  let startRsiH = 0;
  const MIN_BTC = 150;
  const MIN_RSI = 48;

  const onMove = (ev) => {
    if(!dragging) return;
    const dy = ev.clientY - startY;
    const total = startBtcH + startRsiH;
    let nextBtc = startBtcH + dy;
    nextBtc = Math.max(MIN_BTC, Math.min(total - MIN_RSI, nextBtc));
    const nextRsi = Math.max(MIN_RSI, total - nextBtc);
    btcEl.style.height = `${Math.round(nextBtc)}px`;
    rsiEl.style.height = `${Math.round(nextRsi)}px`;
  };
  const stopDrag = () => {
    if(!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', stopDrag);
  };
  splitEl.addEventListener('pointerdown', (ev) => {
    ev.preventDefault();
    dragging = true;
    startY = ev.clientY;
    startBtcH = btcEl.getBoundingClientRect().height;
    startRsiH = rsiEl.getBoundingClientRect().height;
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', stopDrag, { once: true });
  });
}
initChartSplitter();

function initToWinDrag(){
  const panel = document.getElementById('chart-to-win');
  if(!panel || !btcEl) return;
  if(panel.classList.contains('docked')) return;
  const STORE_KEY = 'ui.chart_to_win_pos.v2';
  const PAD = 6;
  let dragging = false;
  let startClientX = 0;
  let startClientY = 0;
  let startLeft = 0;
  let startTop = 0;

  const clampPos = (left, top) => {
    const maxLeft = Math.max(PAD, window.innerWidth - panel.offsetWidth - PAD);
    const maxTop = Math.max(PAD, window.innerHeight - panel.offsetHeight - PAD);
    const nx = Math.min(maxLeft, Math.max(PAD, left));
    const ny = Math.min(maxTop, Math.max(PAD, top));
    return { left: nx, top: ny };
  };
  const applyPos = (left, top) => {
    const p = clampPos(left, top);
    panel.style.left = `${Math.round(p.left)}px`;
    panel.style.top = `${Math.round(p.top)}px`;
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  };
  const savePos = () => {
    if(!panel.style.left || !panel.style.top) return;
    const x = Number.parseInt(panel.style.left, 10);
    const y = Number.parseInt(panel.style.top, 10);
    if(!Number.isFinite(x) || !Number.isFinite(y)) return;
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ x, y })); } catch(_e) {}
  };
  const loadPos = () => {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if(!raw) return false;
      const p = JSON.parse(raw);
      if(!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return false;
      applyPos(p.x, p.y);
      return true;
    } catch(_e) {
      return false;
    }
  };

  const onMove = (ev) => {
    if(!dragging) return;
    const dx = ev.clientX - startClientX;
    const dy = ev.clientY - startClientY;
    applyPos(startLeft + dx, startTop + dy);
  };
  const onUp = () => {
    if(!dragging) return;
    dragging = false;
    panel.classList.remove('dragging');
    document.body.style.userSelect = '';
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    savePos();
  };

  panel.addEventListener('pointerdown', (ev) => {
    if(ev.button !== 0) return;
    ev.preventDefault();
    const rect = panel.getBoundingClientRect();
    if(!panel.style.left || !panel.style.top){
      applyPos(rect.left, rect.top);
    }
    dragging = true;
    panel.classList.add('dragging');
    startClientX = ev.clientX;
    startClientY = ev.clientY;
    startLeft = Number.parseInt(panel.style.left, 10) || 0;
    startTop = Number.parseInt(panel.style.top, 10) || 0;
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp, { once: true });
  });

  requestAnimationFrame(() => {
    if(!loadPos()){
      const root = btcEl.getBoundingClientRect();
      const defLeft = Math.max(PAD, root.right - panel.offsetWidth - 10);
      const defTop = Math.max(PAD, root.bottom - panel.offsetHeight - 10);
      applyPos(defLeft, defTop);
    }
  });
  window.addEventListener('resize', () => {
    if(panel.style.left && panel.style.top){
      applyPos(Number.parseInt(panel.style.left, 10) || 0, Number.parseInt(panel.style.top, 10) || 0);
    }
  });
}
initToWinDrag();

function initQuoteFloatDrag(){
  const panel = document.getElementById('quote-float');
  if(!panel) return;
  const STORE_KEY = 'ui.quote_float_pos.v1';
  const PAD = 6;
  let dragging = false;
  let startClientX = 0;
  let startClientY = 0;
  let startLeft = 0;
  let startTop = 0;

  const clampPos = (left, top) => {
    const maxLeft = Math.max(PAD, window.innerWidth - panel.offsetWidth - PAD);
    const maxTop = Math.max(PAD, window.innerHeight - panel.offsetHeight - PAD);
    return {
      left: Math.min(maxLeft, Math.max(PAD, left)),
      top: Math.min(maxTop, Math.max(PAD, top)),
    };
  };
  const applyPos = (left, top) => {
    const p = clampPos(left, top);
    panel.style.left = `${Math.round(p.left)}px`;
    panel.style.top = `${Math.round(p.top)}px`;
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
  };
  const savePos = () => {
    const x = Number.parseInt(panel.style.left, 10);
    const y = Number.parseInt(panel.style.top, 10);
    if(!Number.isFinite(x) || !Number.isFinite(y)) return;
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ x, y })); } catch(_e) {}
  };
  const loadPos = () => {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if(!raw) return false;
      const p = JSON.parse(raw);
      if(!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return false;
      applyPos(p.x, p.y);
      return true;
    } catch(_e) {
      return false;
    }
  };

  const onMove = (ev) => {
    if(!dragging) return;
    const dx = ev.clientX - startClientX;
    const dy = ev.clientY - startClientY;
    applyPos(startLeft + dx, startTop + dy);
  };
  const onUp = () => {
    if(!dragging) return;
    dragging = false;
    panel.classList.remove('dragging');
    document.body.style.userSelect = '';
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    savePos();
  };

  const onPointerDown = (ev) => {
    if(ev.button !== 0) return;
    ev.preventDefault();
    dragging = true;
    panel.classList.add('dragging');
    if(!panel.style.left || !panel.style.top){
      const rect = panel.getBoundingClientRect();
      applyPos(rect.left, rect.top);
    }
    startClientX = ev.clientX;
    startClientY = ev.clientY;
    startLeft = Number.parseInt(panel.style.left, 10) || 0;
    startTop = Number.parseInt(panel.style.top, 10) || 0;
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp, { once: true });
  };
  panel.addEventListener('pointerdown', onPointerDown);

  if(!loadPos()){
    const r = panel.getBoundingClientRect();
    applyPos(r.left, r.top);
  }
  window.addEventListener('resize', () => {
    applyPos(Number.parseInt(panel.style.left, 10) || 0, Number.parseInt(panel.style.top, 10) || 0);
    savePos();
  });
}
initQuoteFloatDrag();

let syncingRange = false;
const syncToRsi = (range) => {
  if (syncingRange || !range) return;
  syncingRange = true;
  RC.timeScale().setVisibleLogicalRange(range);
  syncingRange = false;
};
BC.timeScale().subscribeVisibleLogicalRangeChange(syncToRsi);

// CMD toggle
document.addEventListener('keydown', e => {
  if(e.ctrlKey && e.key === '\\'){
    e.preventDefault();
    document.getElementById('cmd-wrap').classList.toggle('visible');
  }
});

// state
let tf='1m', leftLogMode='sys', lastSeq=-1, curTarget='current';
let lastShownQuotes={upBid:'–',upAsk:'–',downBid:'–',downAsk:'–'};
let stateReqInFlight=false;
let chartReqInFlight=false;
let latestState=null;
let chartBtcRows=[];
let logRenderPaused=false;
let logFreezeUntilTs=0;
let toastBootstrapped=false;
let tradeToastSeen=0;
let buyToastSeen=0;
let sellToastSeen=0;
const fp = v => v!=null ? Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '–';
const getUsd = () => parseFloat(document.getElementById('usd').value)||1;
const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
const normPx = v => {
  const s = String(v ?? '').trim();
  return s && s !== '-' ? s : null;
};
const stickyPx = (key, nextVal) => {
  if (nextVal) { lastShownQuotes[key] = nextVal; return nextVal; }
  return lastShownQuotes[key] || '–';
};
const toProbPrice = raw => {
  const n = parseCentText(raw);
  if(n == null || !Number.isFinite(n) || n <= 0) return null;
  return n > 1 ? (n / 100) : n;
};
const fmtMoney = v => '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
function toWinText(askText, usdAmt, pos){
  const usd = Number(usdAmt);
  const px = toProbPrice(askText);
  const hasEst = Number.isFinite(usd) && usd > 0 && Number.isFinite(px) && px > 0 && px < 1;
  const shares = Number(pos?.shares || 0);
  const usdIn = Number(pos?.usd_in || 0);
  const hasPos = Number.isFinite(shares) && shares > 0 && Number.isFinite(usdIn) && usdIn >= 0;
  if(!hasEst && !hasPos) return '–';
  let est = 'EST –';
  if(hasEst){
    const estPayout = usd / px;
    const estProfit = estPayout - usd;
    const estPnlTxt = (estProfit >= 0 ? '+' : '-') + fmtMoney(Math.abs(estProfit));
    est = `EST <b>${fmtMoney(estPayout)}</b> <span>${estPnlTxt}</span>`;
  }
  let posTxt = 'POS –';
  if(hasPos){
    const posPayout = shares * 1.0;
    const posProfit = posPayout - usdIn;
    const posPnlTxt = (posProfit >= 0 ? '+' : '-') + fmtMoney(Math.abs(posProfit));
    posTxt = `POS <b>${fmtMoney(posPayout)}</b> <span>${posPnlTxt}</span>`;
  }
  return `${est}<br>${posTxt}`;
}
function renderToWinPanel(upAsk, dnAsk, usdAmt, upPos, dnPos){
  const panel = document.getElementById('chart-to-win');
  if(!panel) return;
  const rows = panel.querySelectorAll('.ctw-row .ctw-val');
  if(!rows || rows.length < 2) return;
  rows[0].innerHTML = toWinText(upAsk, usdAmt, upPos);
  rows[1].innerHTML = toWinText(dnAsk, usdAmt, dnPos);
}
const toIntCent = v => {
  const n = Number(v);
  if(!Number.isFinite(n) || n <= 0) return null;
  return Math.max(1, Math.min(99, Math.round(n)));
};
const parseCentText = raw => {
  const s = String(raw ?? '').replace(/[^\d.]/g, '').trim();
  if(!s) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};
const fmtOrderPriceCent = raw => {
  const v = Number(raw);
  if(!Number.isFinite(v) || v <= 0) return '?';
  // API may return price as decimal probability (0.17) or cents (17).
  const cent = v <= 1 ? (v * 100) : v;
  return `${Math.round(cent)}¢`;
};
const getOrderId = (o) => String(
  o?.id ?? o?.order_id ?? o?.orderID ?? o?.orderId ?? o?.oid ?? ''
).trim();
const getOrderSize = (o) => {
  const v = o?.size ?? o?.original_size ?? o?.amount ?? o?.qty ?? o?.quantity;
  if(v == null || String(v).trim() === '') return '?';
  return String(v);
};
const getOrderStatus = (o) => String(o?.status ?? o?.state ?? o?.order_status ?? '?').slice(0,8);
const getOrderFingerprint = (o) => JSON.stringify({
  id: getOrderId(o),
  side: String(o?.side ?? o?.type ?? '').trim().toLowerCase(),
  status: String(o?.status ?? o?.state ?? o?.order_status ?? '').trim(),
  price: String(o?.price ?? o?.limit_price ?? o?.avg_price ?? '').trim(),
  size: String(o?.size ?? o?.original_size ?? o?.remaining_size ?? o?.amount ?? o?.qty ?? o?.quantity ?? '').trim(),
  matched: String(o?.matched_size ?? o?.filled_size ?? o?.filled ?? '').trim(),
});

function getTokenForSide(d, side){
  const tgt = d?.target_market || 'current';
  if(tgt === 'next') return side === 'up' ? d?.next_up_token : d?.next_down_token;
  if(tgt === 'previous') return side === 'up' ? d?.prev_up_token : d?.prev_down_token;
  return side === 'up' ? d?.up_token : d?.down_token;
}
function getLimitSellQuotes(d, side){
  const view = targetView(d || {});
  const tgt = d?.target_market || 'current';
  const bidText = side === 'up'
    ? (tgt === 'current' ? (normPx(d?.ob_up_bid) || normPx(d?.up_bid)) : normPx(view.upBid))
    : (tgt === 'current' ? (normPx(d?.ob_down_bid) || normPx(d?.down_bid)) : normPx(view.downBid));
  const askText = side === 'up'
    ? (tgt === 'current' ? (normPx(d?.ob_up_ask) || normPx(d?.up_ask)) : normPx(view.upAsk))
    : (tgt === 'current' ? (normPx(d?.ob_down_ask) || normPx(d?.down_ask)) : normPx(view.downAsk));
  return { bid: toIntCent(parseCentText(bidText)), ask: toIntCent(parseCentText(askText)) };
}
function syncLimitSellPanel(){
  const rows = [
    { side:'up', infoId:'ls-up-info', submitId:'ls-up-submit', priceId:'ls-up-price' },
    { side:'down', infoId:'ls-down-info', submitId:'ls-down-submit', priceId:'ls-down-price' },
  ];
  rows.forEach((r)=>{
    const infoEl = document.getElementById(r.infoId);
    const submitEl = document.getElementById(r.submitId);
    const priceEl = document.getElementById(r.priceId);
    if(!infoEl || !submitEl || !priceEl) return;
    if(!latestState){
      infoEl.textContent = 'no state';
      submitEl.disabled = true;
      return;
    }
    const tok = getTokenForSide(latestState, r.side);
    const pos = (tok && latestState.positions) ? latestState.positions[String(tok)] : null;
    const shares = Number(pos?.shares || 0);
    const hasPos = Number.isFinite(shares) && shares > 0;
    submitEl.disabled = !hasPos;
    infoEl.textContent = hasPos ? `${shares.toFixed(4)} sh` : 'no pos';
    const cur = toIntCent(priceEl.value);
    if(cur == null){
      const q = getLimitSellQuotes(latestState, r.side);
      const suggested = q.bid ?? q.ask;
      if(suggested != null) priceEl.value = String(suggested);
    }
  });
}
function placeQuickLimitSell(side){
  if(!latestState){ alert('State not ready'); return; }
  const tok = getTokenForSide(latestState, side);
  const pos = (tok && latestState.positions) ? latestState.positions[String(tok)] : null;
  const totalShares = Number(pos?.shares || 0);
  if(!(totalShares > 0)){ alert(`No ${side.toUpperCase()} position on selected target`); return; }
  const priceEl = document.getElementById(side === 'up' ? 'ls-up-price' : 'ls-down-price');
  const cent = toIntCent(priceEl?.value);
  if(cent == null){ alert('Invalid limit price (1-99 cents)'); return; }
  const qty = Math.floor(totalShares * 10000) / 10000;
  if(!(qty > 0)){ alert('Invalid sell size'); return; }
  const cmdSide = side === 'up' ? 'su' : 'sd';
  cmd(`${cmdSide} ${cent} ${qty}`);
}
function cmd(c){
  fetch('/api/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cmd:c})})
    .then(r=>r.json()).then(d=>{if(!d.ok)console.warn('CMD ERR',d.error||d);})
    .catch(e=>console.warn('[cmd error]',e));
}
function sendRaw(){
  const v=document.getElementById('cmd-in').value.trim();
  if(!v)return; cmd(v); document.getElementById('cmd-in').value='';
}
function mktBuy(side){ cmd(`${side === 'up' ? 'bu' : 'bd'} ${getUsd()}`); }
function mktBuyNext(side){ cmd(`bn${side[0]} ${getUsd()}`); }
function limBuy(side){
  const p=document.getElementById('lp').value.trim();
  const u=document.getElementById('lu').value.trim()||getUsd();
  if(!p){alert('Enter limit price (cents)');return;}
  cmd(`l${side} ${p} ${u}`);
}
function updateLimitMeta(){
  const meta=document.getElementById('lim-meta');
  if(!meta) return;
  const cent=toIntCent(document.getElementById('lp')?.value);
  const usdRaw=document.getElementById('lu')?.value;
  const usd=(String(usdRaw ?? '').trim()==='') ? getUsd() : Number(usdRaw);
  if(cent==null || !Number.isFinite(usd) || usd<=0){
    meta.textContent='Est shares: –';
    return;
  }
  const price=cent/100;
  const shares=usd/price;
  const ok=shares>=5;
  meta.innerHTML=`Est shares: <span class="${ok?'ok':'warn'}">${shares.toFixed(4)} sh</span> · min 5 sh ${ok?'OK':'(LOW)'}`;
}
function toggleDry(){
  const cur=document.getElementById('tb-mode').textContent;
  cmd(cur==='DRY'?'dry off':'dry on');
}
function setTf(t){
  tf=t;
  document.getElementById('btn30s').classList.toggle('on',t==='30s');
  document.getElementById('btn1m').classList.toggle('on',t==='1m');
  document.getElementById('btn5m').classList.toggle('on',t==='5m');
  chartBtcRows = [];
  CS.setData([]);
  PTB.setData([]);
  fetchChart();
}
function setU(v){
  document.getElementById('usd').value=v;
  document.querySelectorAll('.pre').forEach(b=>b.classList.toggle('on',parseFloat(b.textContent)===v));
  updateLimitMeta();
}
function setLeftLog(m){
  leftLogMode=m;
  document.getElementById('llt-sys').classList.toggle('on',m==='sys');
  document.getElementById('llt-tr').classList.toggle('on',m==='trade');
}
function markLogFreeze(ms=3500){
  logFreezeUntilTs = Math.max(logFreezeUntilTs, Date.now() + Math.max(200, ms));
}
function hasSelectionInside(el){
  if(!el) return false;
  const sel = window.getSelection ? window.getSelection() : null;
  if(!sel || sel.rangeCount===0 || sel.isCollapsed) return false;
  const a = sel.anchorNode, f = sel.focusNode;
  return (a && el.contains(a)) || (f && el.contains(f));
}
function updatePauseButtons(){
  const txt = logRenderPaused ? 'RESUME' : 'PAUSE';
  const l = document.getElementById('log-pause-left');
  if(l){ l.textContent = txt; l.classList.toggle('on', logRenderPaused); }
}
function toggleLogPause(){
  logRenderPaused = !logRenderPaused;
  if(!logRenderPaused) markLogFreeze(200);
  updatePauseButtons();
}
async function copyLogPanel(elId){
  const el = document.getElementById(elId);
  if(!el) return;
  const rows = Array.from(el.querySelectorAll('.ll'));
  const txt = rows.length ? rows.map(n=>n.textContent||'').join('\n') : (el.textContent||'').trim();
  if(!txt) return;
  try{
    await navigator.clipboard.writeText(txt);
  }catch(_e){
    const ta = document.createElement('textarea');
    ta.value = txt;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
  markLogFreeze(5000);
}
function setTarget(t){
  curTarget=t;
  cmd(`target ${t}`);
  ['cur','nxt','prv'].forEach(k=>document.getElementById('mb-'+k).classList.remove('on'));
  const map={current:'cur',next:'nxt',previous:'prv'};
  document.getElementById('mb-'+map[t]).classList.add('on');
  if(t !== 'current'){
    lastShownQuotes={upBid:'–',upAsk:'–',downBid:'–',downAsk:'–'};
    const ub = document.getElementById('ob-ub');
    const ua = document.getElementById('ob-ua');
    const db = document.getElementById('ob-db');
    const da = document.getElementById('ob-da');
    if(ub) ub.textContent = '–';
    if(ua) ua.textContent = '–';
    if(db) db.textContent = '–';
    if(da) da.textContent = '–';
  }
}
document.getElementById('lp').addEventListener('input', updateLimitMeta);
document.getElementById('lu').addEventListener('input', updateLimitMeta);
document.getElementById('usd').addEventListener('input', updateLimitMeta);

function logClass(line){
  const l=String(line || '');
  if(/CASHOUT|SELL|CLOSED|PARTIAL/i.test(l)) return 'trade';
  if(/BUY|fill|MKT|queued/i.test(l)) return 'ok';
  if(/error|fail|block|stale|invalid|\?/i.test(l)) return 'err';
  return '';
}
function renderLogPanel(elId, lines){
  const el=document.getElementById(elId);
  if(!el) return;
  if(logRenderPaused) return;
  if(Date.now() < logFreezeUntilTs) return;
  if(el.dataset.sel==='1' || hasSelectionInside(el)) return;
  const rows=Array.isArray(lines)?lines:[];
  const wasNearBottom=(el.scrollHeight-el.scrollTop-el.clientHeight)<24;
  if(!rows.length){ el.innerHTML='<div class="empty-note">No log entries yet.</div>'; return; }
  el.innerHTML=rows.slice(-120).map(l=>`<div class="ll ${logClass(l)}">${esc(l)}</div>`).join('');
  if(wasNearBottom || el.dataset.init!=='1'){ el.scrollTop=el.scrollHeight; el.dataset.init='1'; }
}
function renderTradeSummary(tradeRows){
  const rows=Array.isArray(tradeRows)?tradeRows:[];
  let lastBuy='–'; let lastClose='–'; let closePnl=null;
  for(let i=rows.length-1;i>=0;i--){
    const line=String(rows[i]||'');
    if(lastBuy==='–' && /\bBUY\b/i.test(line)) lastBuy=line;
    if(lastClose==='–' && /\bSELL\b/i.test(line) && /\b(CLOSED|PARTIAL)\b/i.test(line)){
      lastClose=line;
      const m=line.match(/pnl\s*=\s*([+-]?\d+(?:\.\d+)?)/i);
      if(m) closePnl=Number(m[1]);
    }
    if(lastBuy!=='–' && lastClose!=='–') break;
  }
  const buyEl=document.getElementById('last-buy');
  const closeEl=document.getElementById('last-close-pnl');
  if(buyEl){ buyEl.textContent=lastBuy; buyEl.title=lastBuy; }
  if(closeEl){
    closeEl.textContent=lastClose; closeEl.title=lastClose; closeEl.className='lt-v';
    if(Number.isFinite(closePnl)) closeEl.classList.add(closePnl >= 0 ? 'pos' : 'neg');
  }
}
function isBuyFailLine(line){
  const l=String(line||'');
  if(!/\bBUY\b/i.test(l)) return false;
  return /(no-fill|error|blocked|unresolved)/i.test(l);
}
function isBuySuccessLine(line){
  const l=String(line||'');
  if(!/\bBUY\b/i.test(l)) return false;
  return /(fill@|GTC fill@|DRY fill@)/i.test(l);
}
function isSellSuccessLine(line){
  const l=String(line||'');
  if(!/\bSELL\b/i.test(l)) return false;
  return /\bpnl\s*=\s*[+-]?\d+(\.\d+)?/i.test(l);
}
function parseBuySuccess(line){
  const l=String(line||'');
  const sideMatch=l.match(/\bBUY\s+(UP|DOWN)\b/i);
  const side=sideMatch?sideMatch[1].toUpperCase():'BUY';
  const priceMatch=l.match(/fill@\s*([0-9.]+¢)/i);
  const price=priceMatch?priceMatch[1]:'–';
  const shMatch=l.match(/sh=([0-9.]+)/i);
  const shares=shMatch?shMatch[1]:'–';
  const usdMatch=l.match(/usd~?\$([0-9.]+)/i);
  const usd=usdMatch?usdMatch[1]:'–';
  return { side, price, shares, usd };
}
function parseSellSuccess(line){
  const l=String(line||'');
  const sideMatch=l.match(/\bSELL\s+(UP|DOWN)\b/i);
  const side=sideMatch?sideMatch[1].toUpperCase():'SELL';
  const pnlMatch=l.match(/pnl\s*=\s*([+-]?\d+(?:\.\d+)?)/i);
  const pnl=pnlMatch?Number(pnlMatch[1]):null;
  const pxMatch=l.match(/px=([0-9.]+¢)/i);
  const px=pxMatch?pxMatch[1]:'–';
  const shMatch=l.match(/sh=([0-9.]+)/i);
  const shares=shMatch?shMatch[1]:'–';
  return { side, pnl, px, shares };
}
function pushToast(msg, kind=''){
  const host=document.getElementById('toast-stack');
  if(!host) return;
  const el=document.createElement('div');
  el.className='toast'+(kind?` ${kind}`:'');
  el.textContent=String(msg||'').slice(0,220);
  host.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{
    el.classList.remove('show');
    setTimeout(()=>{ if(el.parentNode) el.parentNode.removeChild(el); }, 220);
  }, 3400);
}
function emitBuyFailToasts(tradeRows){
  const rows=Array.isArray(tradeRows)?tradeRows:[];
  if(!toastBootstrapped){
    tradeToastSeen=rows.length;
    buyToastSeen=rows.length;
    sellToastSeen=rows.length;
    toastBootstrapped=true;
    return;
  }
  if(rows.length<tradeToastSeen) tradeToastSeen=0;
  if(rows.length===tradeToastSeen) return;
  const fresh=rows.slice(tradeToastSeen);
  tradeToastSeen=rows.length;
  fresh.forEach(line=>{
    if(isBuyFailLine(line)) pushToast(line,'err');
  });
}
function emitSellSuccessToasts(tradeRows){
  const rows=Array.isArray(tradeRows)?tradeRows:[];
  if(rows.length<sellToastSeen) sellToastSeen=0;
  if(rows.length===sellToastSeen) return;
  const fresh=rows.slice(sellToastSeen);
  sellToastSeen=rows.length;
  fresh.forEach(line=>{
    if(!isSellSuccessLine(line)) return;
    const info=parseSellSuccess(line);
    if(!Number.isFinite(info.pnl)) return;
    const msg=`${info.side} SELL ${info.px} | ${info.shares} sh | ${info.pnl>=0?'+':''}${info.pnl.toFixed(4)}`;
    pushToast(msg, info.pnl>=0 ? 'win' : 'lose');
  });
}
function emitBuySuccessToasts(tradeRows){
  const rows=Array.isArray(tradeRows)?tradeRows:[];
  if(rows.length<buyToastSeen) buyToastSeen=0;
  if(rows.length===buyToastSeen) return;
  const fresh=rows.slice(buyToastSeen);
  buyToastSeen=rows.length;
  fresh.forEach(line=>{
    if(!isBuySuccessLine(line)) return;
    const info=parseBuySuccess(line);
    const msg=`${info.side} BUY ${info.price} | ${info.shares} sh | $${info.usd}`;
    pushToast(msg, 'win');
  });
}

// countdown bar
let candleIntervalEnd = 0;
let candleTotalDuration = 300;
function updateCountdownBar(){
  const now = Math.floor(Date.now()/1000);
  const rem = Math.max(0, candleIntervalEnd - now);
  const pct = Math.min(100, (rem / candleTotalDuration) * 100);
  document.getElementById('countdown-bar').style.width = pct + '%';
}
setInterval(updateCountdownBar, 1000);

function fetchChart(){
  if(chartReqInFlight) return;
  chartReqInFlight = true;
  fetch(`/api/chart?tf=${tf}`).then(r=>r.json()).then(d=>{
    if(!d.ok)return;
    const rows = (d.btc||[]);
    const c=rows.map(r=>({time:r[0],open:r[1],high:r[2],low:r[3],close:r[4]}));
    chartBtcRows = c.slice(-220);
    CS.setData(chartBtcRows);
    const ptb=rows.filter(r=>r[5]!=null).map(r=>({time:r[0],value:r[5]}));
    PTB.setData(ptb);
    const ts=rows.map(r=>r[0]);
    const toS=arr=>arr.map((v,i)=>v!=null?{time:ts[i],value:v}:null).filter(Boolean);
    const I=d.indicators||{};
    if(I.ema9)    E9.setData(toS(I.ema9));
    if(I.ema21)   E21.setData(toS(I.ema21));
    if(I.bb_upper)BBU.setData(toS(I.bb_upper));
    if(I.bb_mid)  BBM.setData(toS(I.bb_mid));
    if(I.bb_lower)BBL.setData(toS(I.bb_lower));
    if(I.rsi7){
      RSI.setData(toS(I.rsi7));
      if(ts.length){
        ROB.setData([{time:ts[0],value:60},{time:ts[ts.length-1],value:60}]);
        ROS.setData([{time:ts[0],value:40},{time:ts[ts.length-1],value:40}]);
      }
    }
    const L=d.live||{}, sig=d.signal||'NEUTRAL', rsn=(d.signal_reasons||[]).join(' · ');
    document.getElementById('i-e9').textContent  = L.ema9  ? fp(L.ema9)  : '–';
    document.getElementById('i-e21').textContent = L.ema21 ? fp(L.ema21) : '–';
    const rEl=document.getElementById('i-rsi');
    rEl.textContent=L.rsi7!=null?L.rsi7.toFixed(1):'–';
    rEl.className='iv'+(L.rsi7>60?' g':L.rsi7<40?' r':'');
    document.getElementById('i-bbu').textContent = L.bb_upper ? fp(L.bb_upper) : '–';
    document.getElementById('i-bbm').textContent = L.bb_mid   ? fp(L.bb_mid)   : '–';
    document.getElementById('i-bbl').textContent = L.bb_lower ? fp(L.bb_lower) : '–';
    const dv=L.ptb_dist_pct;
    const dEl=document.getElementById('i-ptbd');
    dEl.textContent=dv!=null?(dv>=0?'+':'')+dv.toFixed(3)+'%':'–';
    dEl.className='iv'+(dv>0.15?' g':dv<-0.15?' r':Math.abs(dv||0)<0.05?' y':'');
    const sEl=document.getElementById('i-sig');
    sEl.textContent=sig+(rsn?' ('+rsn+')':'');
    sEl.className='sig s-'+sig;
    const tSig=document.getElementById('t-sig');
    tSig.textContent=sig; tSig.className='sig s-'+sig;
    document.getElementById('ctrl-sig-label').textContent=rsn||'–';
    syncToRsi(BC.timeScale().getVisibleLogicalRange());
  }).catch(e=>console.warn('[fetchChart error]',e)).finally(()=>{ chartReqInFlight=false; });
}

function applyLiveBtcToChart(nowPx){
  const px = Number(nowPx);
  if(!Number.isFinite(px) || px <= 0) return;
  const sec = tf === '5m' ? 300 : (tf === '30s' ? 30 : 60);
  const nowTs = Math.floor(Date.now()/1000);
  const bucket = Math.floor(nowTs / sec) * sec;
  if(!chartBtcRows.length){
    chartBtcRows = [{time:bucket,open:px,high:px,low:px,close:px}];
    CS.setData(chartBtcRows);
    return;
  }
  const last = chartBtcRows[chartBtcRows.length - 1];
  if(last.time === bucket){
    last.high = Math.max(Number(last.high), px);
    last.low = Math.min(Number(last.low), px);
    last.close = px;
  } else if(bucket > Number(last.time)){
    const prevClose = Number(last.close);
    chartBtcRows.push({time:bucket, open:prevClose, high:px, low:px, close:px});
    if(chartBtcRows.length > 220) chartBtcRows = chartBtcRows.slice(-220);
  } else {
    return;
  }
  CS.setData(chartBtcRows);
}

function targetView(d){
  const target=d.target_market||'current';
  if(target==='next') return { slug:d.next_slug||'(next not ready)', intervalEnd:d.next_interval_end||0, upBid:d.next_up_bid||'–', upAsk:d.next_up_ask||'–', downBid:d.next_down_bid||'–', downAsk:d.next_down_ask||'–', ready:!!d.next_ready, label:'NEXT' };
  if(target==='previous') return { slug:d.prev_slug||'(no previous market)', intervalEnd:d.prev_interval_end||0, upBid:d.prev_up_bid||'–', upAsk:d.prev_up_ask||'–', downBid:d.prev_down_bid||'–', downAsk:d.prev_down_ask||'–', ready:!!d.prev_ready, label:'PREV' };
  return { slug:d.current_slug||'–', intervalEnd:d.interval_end||0, upBid:d.up_bid||'–', upAsk:d.up_ask||'–', downBid:d.down_bid||'–', downAsk:d.down_ask||'–', ready:true, label:'CURRENT' };
}

function posRow(posEl, entEl, pnlEl, posText, entryText, pnlValue){
  posEl.textContent=posText||'–'; entEl.textContent=entryText||'–';
  if(typeof pnlValue==='number'&&Number.isFinite(pnlValue)){
    pnlEl.textContent=(pnlValue>=0?'+':'')+pnlValue.toFixed(4);
    pnlEl.className=pnlValue>0.001?'ppos':pnlValue<-0.001?'pneg':'pneu';
  } else { pnlEl.textContent='–'; pnlEl.className='pneu'; }
}

function fetchState(){
  if(stateReqInFlight) return;
  stateReqInFlight=true;
  fetch('/api/state').then(r=>r.json()).then(d=>{
    latestState=d;
    if(d.snapshot_seq===lastSeq) return;
    lastSeq=d.snapshot_seq;
    const view=targetView(d);
    const tgt=d.target_market||'current';
    const rem=Math.max(0,(view.intervalEnd||0)-Math.floor(Date.now()/1000));
    candleIntervalEnd=view.intervalEnd||0;

    document.getElementById('t-slug').textContent=view.slug||'–';
    const rEl=document.getElementById('t-rem');
    rEl.textContent=String(Math.floor(rem/60)).padStart(2,'0')+':'+String(rem%60).padStart(2,'0');
    const wEl=document.getElementById('t-ws');
    wEl.textContent=d.ws_ok?'WS ✓':'WS ✗';
    wEl.className='tb-val'+(d.ws_ok?' g':' r');
    const mEl=document.getElementById('tb-mode');
    mEl.textContent=d.dry_run?'DRY':'LIVE';
    mEl.className=d.dry_run?'mode-dry':'mode-live';
    document.getElementById('t-bal').textContent=d.balance||'–';
    const spnl = d.session_pnl ?? 0;
    const spnlEl = document.getElementById('t-session-pnl');
    if (spnlEl) {
      spnlEl.textContent = (spnl >= 0 ? '+' : '') + '$' + Math.abs(spnl).toFixed(2);
      spnlEl.style.color = spnl > 0 ? '#eeeeee' : spnl < 0 ? '#666666' : '#aaaaaa';
    }
    const wr=Math.round((d.prob_win_rate||0)*100);
    document.getElementById('t-wr').textContent=`${wr}% (${d.prob_win_wins||0}/${d.prob_win_total||0})`;

    const btcNow=d.btc_price_now??d.btc_now;
    const btcPtb=d.btc_price_to_beat??d.btc_ptb;
    applyLiveBtcToChart(btcNow);
    document.getElementById('s-now').textContent=btcNow!=null?'$'+fp(btcNow):'–';
    document.getElementById('s-ptb').textContent=btcPtb!=null?'$'+fp(btcPtb):'–';
    const pre = (d.prob_pre_up!=null)?Math.round(d.prob_pre_up*100):null;
    const live = (d.prob_up!=null)?Math.round(d.prob_up*100):null;
    const preEl=document.getElementById('s-pre');
    const liveEl=document.getElementById('s-live');
    if(preEl) preEl.textContent=pre!=null?`${pre}%`:'–';
    if(liveEl) liveEl.textContent=live!=null?`${live}%`:'–';
    document.getElementById('s-next').textContent=d.next_slug||'(wait)';
    const nrem=Math.max(0,(d.next_interval_end||0)-Math.floor(Date.now()/1000));
    document.getElementById('s-nextt').textContent=nrem>0?String(Math.floor(nrem/60)).padStart(2,'0')+':'+String(nrem%60).padStart(2,'0'):'–';
    const pu=Math.round((d.prob_up||0.5)*100);
    document.getElementById('s-slug2').textContent=view.slug||'BTC/USD';

    if(tgt!==curTarget){
      curTarget=tgt; lastShownQuotes={upBid:'–',upAsk:'–',downBid:'–',downAsk:'–'};
      ['cur','nxt','prv'].forEach(k=>document.getElementById('mb-'+k).classList.remove('on'));
      const map={current:'cur',next:'nxt',previous:'prv'};
      if(map[tgt]) document.getElementById('mb-'+map[tgt]).classList.add('on');
    }
    // Keep NEXT selectable even before token/quote is ready.
    document.getElementById('mb-nxt').disabled=false;
    document.getElementById('mb-prv').disabled=!d.prev_ready;
    let mktInfo='–';
    if(tgt==='next') mktInfo=`NEXT: ${view.slug}`;
    else if(tgt==='previous') mktInfo=`PREV: ${view.slug}`;
    else mktInfo=`${view.slug}`;
    document.getElementById('mkt-info').textContent=mktInfo;

    const np=btcNow, pp=btcPtb;
    document.getElementById('r-now').textContent=np!=null?'$'+fp(np):'–';
    document.getElementById('r-ptb').textContent=pp!=null?'$'+fp(pp):'–';
    if(np!=null&&pp!=null){
      const diff=np-pp;
      const distPct=(diff/pp)*100;
      const dEl=document.getElementById('r-dist');
      dEl.textContent=`${diff>=0?'+':'-'}$${fp(Math.abs(diff))}`;
      dEl.style.color=diff>0?'var(--white)':diff<0?'var(--txt2)':'var(--hi)';
      document.getElementById('ptb-m').style.left=Math.max(5,Math.min(95,50+distPct/0.5*50))+'%';
    } else {
      document.getElementById('r-dist').textContent='–';
      document.getElementById('ptb-m').style.left='50%';
    }

    // probability — topbar compact
    const confPct=Math.round((d.prob_confidence||0)*100);
    document.getElementById('tb-prob-up').textContent=`${pu}%`;
    document.getElementById('tb-prob-dn').textContent=`${100-pu}%`;
    document.getElementById('tb-prob-conf').textContent=`${confPct}%`;

    const upBid=tgt==='current'?(normPx(d.ob_up_bid)||normPx(d.up_bid)):normPx(view.upBid);
    const upAsk=tgt==='current'?(normPx(d.ob_up_ask)||normPx(d.up_ask)):normPx(view.upAsk);
    const dnBid=tgt==='current'?(normPx(d.ob_down_bid)||normPx(d.down_bid)):normPx(view.downBid);
    const dnAsk=tgt==='current'?(normPx(d.ob_down_ask)||normPx(d.down_ask)):normPx(view.downAsk);
    if(tgt === 'current'){
      document.getElementById('ob-ub').textContent=stickyPx('upBid',upBid);
      document.getElementById('ob-ua').textContent=stickyPx('upAsk',upAsk);
      document.getElementById('ob-db').textContent=stickyPx('downBid',dnBid);
      document.getElementById('ob-da').textContent=stickyPx('downAsk',dnAsk);
    } else {
      document.getElementById('ob-ub').textContent=upBid || '–';
      document.getElementById('ob-ua').textContent=upAsk || '–';
      document.getElementById('ob-db').textContent=dnBid || '–';
      document.getElementById('ob-da').textContent=dnAsk || '–';
    }
    const usdCur = getUsd();
    const upTok = getTokenForSide(d, 'up');
    const dnTok = getTokenForSide(d, 'down');
    const upPos = (upTok && d.positions) ? d.positions[String(upTok)] : null;
    const dnPos = (dnTok && d.positions) ? d.positions[String(dnTok)] : null;
    renderToWinPanel(upAsk, dnAsk, usdCur, upPos, dnPos);

    posRow(document.getElementById('ob-up'),document.getElementById('ob-ue'),document.getElementById('ob-upnl'),d.ob_up_pos_usd||d.up_pos_usd,d.ob_up_entry||d.up_entry,d.ob_up_pnl);
    posRow(document.getElementById('ob-dp'),document.getElementById('ob-de'),document.getElementById('ob-dpnl'),d.ob_down_pos_usd||d.down_pos_usd,d.ob_down_entry||d.down_entry,d.ob_down_pnl);

    const buyUpBtn=document.querySelector('.btn-up');
    const buyDnBtn=document.querySelector('.btn-dn');
    if(buyUpBtn){ buyUpBtn.textContent=tgt==='next'?'▲ BUY NEXT UP':'▲ BUY UP'; buyUpBtn.disabled=(tgt==='previous')||(tgt==='next'&&!d.next_ready); }
    if(buyDnBtn){ buyDnBtn.textContent=tgt==='next'?'▼ BUY NEXT DN':'▼ BUY DOWN'; buyDnBtn.disabled=(tgt==='previous')||(tgt==='next'&&!d.next_ready); }
    syncLimitSellPanel();

    const oo=d.open_orders||[];
    document.getElementById('oo-c').textContent=oo.length?`(${oo.length})`:'';
    const ooEl=document.getElementById('oo-list');
    const ooKey=JSON.stringify(oo.map(getOrderFingerprint));
    if(ooEl.dataset.lastKey!==ooKey){
      ooEl.dataset.lastKey=ooKey;
      if(!oo.length){ ooEl.innerHTML='<div class="empty-note">No open orders.</div>'; }
      else {
        ooEl.innerHTML=oo.map(o=>{
          const oid=getOrderId(o);
          const oidShort = oid ? oid.slice(0, 10) : '';
          const rawSide=String(o.side||o.type||'').toLowerCase();
          const s=rawSide==='up'||rawSide==='buy'?'BUY':rawSide==='down'||rawSide==='sell'?'SELL':rawSide.toUpperCase();
          return `<div class="oo-row"><span class="oo-badge ${s==='BUY'?'ob-up':'ob-dn'}">${esc(s||'?')}</span><span class="oo-txt">${esc(fmtOrderPriceCent(o.price))} × ${esc(getOrderSize(o))}sh · ${esc(getOrderStatus(o))}${oidShort ? ' · #' + esc(oidShort) : ''}</span><button class="btn-sm b-sell" onclick="cmd('co ${oid}')" ${oid?'':'disabled'}>✕</button></div>`;
        }).join('');
      }
    }

    const tradeRows=d.trade_log||[];
    emitBuyFailToasts(tradeRows);
    emitBuySuccessToasts(tradeRows);
    emitSellSuccessToasts(tradeRows);
    renderTradeSummary(tradeRows);
    renderLogPanel('left-log-body', leftLogMode==='sys'?(d.log||[]):tradeRows);
  }).catch(e=>console.warn('[fetchState error]',e)).finally(()=>{ stateReqInFlight=false; });
}

fetchState();
fetchChart();
setInterval(fetchState, 150);
setInterval(fetchChart, 400);

['left-log-body'].forEach(id=>{
  const el=document.getElementById(id);
  if(!el) return;
  el.addEventListener('pointerdown', ()=>{ el.dataset.sel='1'; markLogFreeze(6000); });
  el.addEventListener('pointerup', ()=>{ setTimeout(()=>{ el.dataset.sel='0'; }, 160); markLogFreeze(3500); });
  el.addEventListener('mouseleave', ()=>{ if(el.dataset.sel==='1') markLogFreeze(3500); });
  el.addEventListener('copy', ()=>{ markLogFreeze(7000); });
});
document.addEventListener('selectionchange', ()=>{
  const left = document.getElementById('left-log-body');
  if(left && hasSelectionInside(left)){
    markLogFreeze(3500);
  }
});
updatePauseButtons();
updateLimitMeta();
