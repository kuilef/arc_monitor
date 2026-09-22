'use strict';
const $ = id => document.getElementById(id);
let selection = null;
let rows = [];
let requestNumber = 0;
let detailNumber = 0;
let debounce;
const number = (value, digits=1) => value == null ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits:digits}).format(Number(value));
const compact = value => {
  if (value == null) return '—';
  const n=Number(value);
  if (!Number.isFinite(n)) return '—';
  return new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:1}).format(n);
};
const pct = value => value == null ? '—' : number(Number(value)*100)+'%';
const age = value => value>=1440 ? number(value/1440)+' д' : value>=60 ? number(value/60)+' ч' : number(value,0)+' м';
function node(tag, text, className) {
  const n=document.createElement(tag);
  if (text != null) n.textContent=text;
  if (className) n.className=className;
  return n;
}
function svgNode(tag, attributes={}, text=null) {
  const n=document.createElementNS('http://www.w3.org/2000/svg',tag);
  Object.entries(attributes).forEach(([k,v]) => n.setAttribute(k,String(v)));
  if (text!=null) n.textContent=text;
  return n;
}
async function api(path) {
  const r=await fetch(path, {cache:'no-store'});
  if (!r.ok) throw new Error('Ошибка локального API: HTTP '+r.status);
  return r.json();
}
function error(e) { $('error').textContent=e.message; $('error').hidden=false; }
function appendCell(tr, text, sub=null, cls='') {
  const td=node('td');
  td.append(node('span',text,cls));
  if (sub!=null) td.append(node('small',sub));
  tr.append(td);
  return td;
}
function renderTable() {
  $('rows').replaceChildren();
  rows.forEach(r => {
    const tr=node('tr');
    if (r.token===selection) tr.classList.add('selected');
    const first=node('td');
    const button=node('button',r.symbol || r.token.slice(0,10),'token-button');
    button.title=r.token;
    first.append(button,node('small',r.launchpad));
    tr.append(first);
    const stage=appendCell(tr,r.stage,age(r.age_minutes),'stage');
    stage.firstChild.dataset.stage=r.stage;
    appendCell(tr,compact(r.adjusted_volume_usdc),compact(r.raw_volume_usdc)+' raw');
    appendCell(tr,number(r.new_buyers_5m,0),'до этого '+number(r.new_buyers_previous_5m,0));
    appendCell(tr,number(r.buyer_clusters_estimate,0)+' / '+number(r.unique_buyers,0),'funding '+pct(r.funding_coverage));
    appendCell(tr,compact(r.liquidity_usdc),r.liquidity_retention==null?'Нет свежего среза':pct(r.liquidity_retention)+' от пика');
    appendCell(tr,number(r.runner_score,0),null,r.runner_score>=65?'score-good':'score-mid');
    appendCell(tr,number(r.risk_score,0),null,r.risk_score==null?'score-unknown':r.risk_score>=60?'score-good':'score-mid');
    appendCell(tr,number(r.wash_score,0),pct(r.suspicious_volume_share)+' объёма');
    tr.addEventListener('click',()=>showDetail(r.token));
    $('rows').append(tr);
  });
  $('empty').hidden=rows.length>0;
}
function volumeChart() {
  const svg=$('volume-chart'); svg.replaceChildren();
  const data=rows.slice(0,10), max=Math.max(1,...data.map(r=>Number(r.raw_volume_usdc)));
  const width=760/Math.max(1,data.length);
  svg.append(svgNode('line',{x1:35,y1:150,x2:805,y2:150,class:'gridline'}));
  data.forEach((r,i) => {
    const x=40+i*width, bar=Math.min(28,width/3);
    const h=Number(r.raw_volume_usdc)/max*115, a=Number(r.adjusted_volume_usdc)/max*115;
    svg.append(svgNode('rect',{x,y:150-h,width:bar,height:h,class:'bar-raw',rx:2}),
               svgNode('rect',{x:x+bar+3,y:150-a,width:bar,height:a,class:'bar-adjusted',rx:2}),
               svgNode('text',{x,y:173,class:'chart-label'},r.symbol || r.token.slice(0,8)),
               svgNode('text',{x,y:Math.max(15,140-h),class:'chart-label'},compact(r.raw_volume_usdc)));
  });
}
function priceChart(series) {
  const svg=$('price-chart'); svg.replaceChildren();
  const data=series.filter(p=>Number.isFinite(Number(p.price_usdc)) && Number.isFinite(Date.parse(p.time)));
  if (!data.length) { svg.append(svgNode('text',{x:20,y:70,class:'chart-label'},'Нет наблюдаемых сделок')); return; }
  const values=data.map(p=>Number(p.price_usdc)), times=data.map(p=>Date.parse(p.time));
  const lo=Math.min(...values),hi=Math.max(...values), t0=times[0], span=Math.max(1,times[times.length-1]-t0);
  const y=v=>125-(v-lo)/Math.max(1e-18,hi-lo)*90;
  [lo,(lo+hi)/2,hi].forEach(v=>{
    svg.append(svgNode('line',{x1:58,y1:y(v),x2:448,y2:y(v),class:'gridline'}),
      svgNode('text',{x:1,y:y(v)+3,class:'chart-label'},Number(v).toPrecision(3)));
  });
  const points=data.map((p,i)=>(60+(times[i]-t0)/span*385)+','+y(values[i])).join(' ');
  svg.append(svgNode('polyline',{points,class:'price-line'}));
  if(data.length===1)svg.append(svgNode('circle',{cx:60,cy:y(values[0]),r:3,class:'price-point'}));
  svg.append(svgNode('text',{x:60,y:150,class:'chart-label'},data[0].time.slice(5,16).replace('T',' ')+' UTC'),
    svgNode('text',{x:330,y:150,class:'chart-label'},data[data.length-1].time.slice(11,16)+' UTC'));
}
const missingNames={contract_risk:'Проверка контракта',complete_holders:'Полный срез держателей',fresh_liquidity:'Свежая ликвидность всех известных пулов',recent_trades:'Сделки за последние 30 минут'};
async function showDetail(token) {
  selection=token; renderTable();
  const id=++detailNumber;
  try {
    const {metrics:m,series}=await api('/api/tokens/'+encodeURIComponent(token));
    if (id!==detailNumber) return;
    $('detail-title').textContent=m.symbol || 'Без символа';
    $('detail-address').textContent=m.token;
    $('detail-body').hidden=false;
    $('detail-scores').replaceChildren();
    [['Runner',m.runner_score],['Risk',m.risk_score],['Wash',m.wash_score]].forEach(([label,value])=>{
      const el=node('div',label,'score-card'+(value==null?' unknown':''));
      el.append(node('strong',number(value,0))); $('detail-scores').append(el);
    });
    priceChart(series);
    const creator=m.creator_history;
    const facts=[['Цена последней сделки',m.price_usdc==null?'—':Number(m.price_usdc).toPrecision(5)],
      ['Капитализация',compact(m.market_cap_usdc)],['FDV (не капитализация)',compact(m.fdv_usdc)],
      ['Чистый buy flow / 30 м',compact(m.net_buy_flow_usdc)],['Держатели',number(m.holders_count,0)],
      ['Top-10 / eligible supply',pct(m.top10_eligible_share)],['Удержание покупателей',pct(m.buyer_retention_30m)],
      ['Просадка от наблюд. пика',pct(m.drawdown_from_observed_peak)],
      ['Другие запуски creator',creator?number(creator.observed_other_launches,0):'—'],
      ['Survival 24 ч у creator',creator?creator.survived_24h+' / '+creator.mature_covered_24h+' зрелых':'—']];
    $('facts').replaceChildren(); facts.forEach(([k,v])=>$('facts').append(node('dt',k),node('dd',v)));
    $('missing').textContent=m.missing_evidence.length?'Не хватает: '+m.missing_evidence.map(k=>missingNames[k]||k).join('; ')+'.':'Все четыре категории свидетельств заполнены. Это не аудит безопасности.';
    $('quality').replaceChildren(); m.quality_notes.forEach(q=>$('quality').append(node('p',q)));
    $('components').textContent=JSON.stringify(m.runner_components,null,2)+'\n'+m.score_version;
  } catch(e) {error(e);}
}
async function refresh() {
  const request=++requestNumber;
  $('refresh').disabled=true;
  try {
    const params=new URLSearchParams({q:$('search').value,stage:$('stage').value,sort:$('sort').value,limit:'100'});
    const [status,eco,tokens]=await Promise.all([api('/api/status'),api('/api/ecosystem'),api('/api/tokens?'+params)]);
    if (request!==requestNumber) return;
    $('error').hidden=true;
    $('mode').textContent=status.mode.toUpperCase();
    $('banner').textContent=status.synthetic?'DEMO · Синтетические сценарии. Это не реальные токены, не рыночные данные и не доказательство прибыльности.':'LIVE DATASET · Наблюдаемые данные. Рейтинги не проверены на прогнозную точность; неполное покрытие ограничивает выводы.';
    const collection=status.collection;
    if (collection) {
      const parts=['launches','trades'].map(k=>{
        const s=collection.streams[k];
        return k+': '+(s.fresh?'свежие':'устарели / ещё не загружены')+
          ', пробелы за 24 ч: '+number(s.gap_seconds/60,0)+' мин';
      });
      const run=collection.collector;
      parts.push('Сбор: '+(run ? (run.unresponsive?'нет heartbeat':run.status) : 'не запускался'));
      const q=collection.quota?.billing_period;
      if(q?.usage && q?.limits) parts.push('Points: '+number(q.usage.points_usage,0)+' / '+number(q.limits.points_limit,0)+' (счётчик может запаздывать)');
      $('collection-health').textContent=status.synthetic?'DEMO · Статус относится к синтетическому набору.':parts.join(' · ');
    }
    $('asof').textContent=status.as_of.replace('T',' ').replace('Z',' UTC');
    $('tokens-count').textContent=number(eco.observed_tokens,0);
    $('traders').textContent=number(eco.active_traders_24h,0);
    $('adjusted').textContent=compact(eco.adjusted_volume_usdc_24h);
    $('raw').textContent=compact(eco.raw_volume_usdc_24h);
    $('cctp').textContent=compact(eco.cctp_net_usdc_24h);
    $('cctp-note').textContent=eco.cctp_coverage?'USDC · учтённые сообщения':'Нет подтверждённого покрытия';
    $('coverage').textContent=eco.trades_coverage?'Интервалы trades / 24 ч: загружены в рамках источника':'Trades / 24 ч: есть пробелы или покрытие не подтверждено';
    rows=tokens.items;
    $('count-label').textContent=tokens.total+' по фильтру'+(tokens.truncated_universe?' · ограничение '+tokens.scanned_tokens:'');
    renderTable();volumeChart();
    if(!selection && rows.length) selection=rows[0].token;
    if(selection) await showDetail(selection);
  } catch(e) {error(e);}
  finally { if(request===requestNumber)$('refresh').disabled=false; }
}
$('refresh').addEventListener('click',refresh);
$('search').addEventListener('input',()=>{clearTimeout(debounce);debounce=setTimeout(refresh,250);});
$('stage').addEventListener('change',refresh);
$('sort').addEventListener('change',refresh);
refresh();
setInterval(()=>{if(!document.hidden && $('mode').textContent==='LIVE')refresh();},30000);
