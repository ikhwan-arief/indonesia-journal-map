import { bubbleRadius as radius } from './metrics.js';

const $ = selector => document.querySelector(selector);
const UNMAPPED = '__unmapped__';
const UNCLASSIFIED = '__none__';
const NO_TYPE = '__unknown__';
const state = { data: null, mode: 'all', allProvinces: false, city: '', field: '', ptype: '', query: '', page: 1, perPage: 25, view: { zoom: 1, x: 0, y: 0 } };
const fmt = new Intl.NumberFormat('en-US');
const pct = new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const PLURALS = { city: 'cities' };
const plural = (count, word) => `${fmt.format(count)} ${count === 1 ? word : (PLURALS[word] || `${word}s`)}`;

// Equirectangular projection with one scale for both axes, so islands keep their real shape.
// View: 94.5°E–141.5°E, 6.5°N–11.5°S inside the 1000×430 SVG viewBox.
const VIEW = { west: 94.5, north: 6.5, scale: 20.4, left: 20.6, top: 31.4 };
const project = ([lon, lat]) => [VIEW.left + (lon - VIEW.west) * VIEW.scale, VIEW.top + (VIEW.north - lat) * VIEW.scale];
const ringPath = ring => ring.map((point, i) => `${i ? 'L' : 'M'}${project(point).map(v => v.toFixed(1)).join(',')}`).join('') + 'Z';

function drawLand(basemap) {
  const neighbors = `<path class="neighbor" d="${basemap.neighbors.map(ringPath).join('')}"/>`;
  const provinces = basemap.provinces.map(region => `<path class="province" d="${region.rings.map(ringPath).join('')}"/>`).join('');
  $('#land').innerHTML = neighbors + provinces;
}

/* ---------- Map modes and publisher types ---------- */
const MODES = {
  all: { title: 'All journals', noun: 'journal', test: () => true, note: '' },
  diamond: { title: 'Diamond OA journals', noun: 'diamond OA journal', test: j => j.diamond === true,
    note: 'Showing <strong>diamond open-access</strong> journals only: listed in DOAJ with no article processing charge and no other author fees.' },
  doaj: { title: 'DOAJ-indexed journals', noun: 'DOAJ-indexed journal', test: j => j.is_in_doaj === true,
    note: 'Showing journals indexed in the <strong>Directory of Open Access Journals (DOAJ)</strong>, per OpenAlex or the DOAJ journal list.' },
};
const TYPES = [
  ['education', 'Education', 'universities, colleges, schools'],
  ['government', 'Government agency', 'ministries, agencies, research bodies, hospitals'],
  ['private', 'Private', 'companies, foundations, associations, societies'],
  ['other', 'Other', 'other organisations'],
  [NO_TYPE, 'Not determined', 'no publisher information'],
];
const typeLabel = key => TYPES.find(([id]) => id === key)?.[1] || key;
function typeKey(journal) { return journal.publisher.type || NO_TYPE; }

/* ---------- Derived data for the current mode ---------- */
let cityById = new Map();
function inMode(journal) { return (MODES[state.mode] || MODES.all).test(journal); }
function modeJournals() { return state.data.journals.filter(inMode); }
function fieldKey(journal) { return journal.field?.id || UNCLASSIFIED; }

function cityCounts(journals) {
  const counts = new Map();
  for (const journal of journals) {
    const id = journal.location.city_id;
    if (!id) continue;
    const row = counts.get(id) || { journals: 0, publishers: new Set() };
    row.journals += 1; row.publishers.add(journal.publisher.id || journal.publisher.name || journal.id);
    counts.set(id, row);
  }
  return counts;
}

/* ---------- Summary ---------- */
function renderKpis(journals, counts) {
  const total = journals.length, mapped = journals.filter(j => j.location.status === 'mapped').length;
  // The first three cards are totals and switch the map mode; the last three follow the current mode.
  const modeCards = [['all', 'Journals'], ['diamond', 'Diamond OA'], ['doaj', 'In DOAJ']].map(([mode, label]) =>
    `<button type="button" class="kpi" data-mode="${mode}" aria-pressed="${state.mode === mode}" title="Show ${MODES[mode].title.toLowerCase()} on the map"><strong>${fmt.format(state.data.journals.filter(MODES[mode].test).length)}</strong><span>${label}</span></button>`);
  const scope = { all: '', diamond: ' · Diamond', doaj: ' · DOAJ' }[state.mode];
  const cards = [
    ['Mapped to a city', fmt.format(mapped)],
    ['Location coverage', `${pct.format(total ? 100 * mapped / total : 0)}%`],
    ['Cities', fmt.format(counts.size)],
  ].map(([label, value]) => `<article class="kpi"><strong>${value}</strong><span>${label}${scope}</span></article>`);
  $('#kpis').innerHTML = modeCards.join('') + cards.join('');
  document.querySelectorAll('#kpis [data-mode]').forEach(button => button.addEventListener('click', () => setMode(button.dataset.mode)));
}

/* ---------- Map ---------- */
function cityLabel(city) { return `${city.name}${city.province ? `, ${city.province}` : ''}`; }
function showTip(event, city, count, total) {
  const tip = $('#tooltip'), rect = $('#map-wrap').getBoundingClientRect();
  const noun = MODES[state.mode].noun;
  tip.innerHTML = `<strong>${esc(cityLabel(city))}</strong>${plural(count.journals, noun)} · ${plural(count.publishers.size, 'publisher')}<br>${(count.journals / total * 100).toFixed(2)}% of the total shown`;
  tip.hidden = false; const box = event.currentTarget.getBoundingClientRect();
  tip.style.left = `${Math.min(rect.width - 245, Math.max(8, box.left - rect.left + box.width / 2))}px`;
  tip.style.top = `${Math.max(8, box.top - rect.top - 72)}px`;
}
function hideTip() { $('#tooltip').hidden = true; }

function drawBubbles(counts, total) {
  const rows = [...counts].map(([id, count]) => ({ city: cityById.get(id), count })).filter(row => row.city)
    .sort((a, b) => b.count.journals - a.count.journals);
  const max = rows[0]?.count.journals || 1;
  $('#bubbles').innerHTML = rows.map(({ city, count }) => { const [cx, cy] = project([city.longitude, city.latitude]);
    return `<g class="bubble${state.city === city.id ? ' active' : ''}" data-city="${esc(city.id)}" tabindex="0" role="button" aria-label="${esc(cityLabel(city))}: ${plural(count.journals, 'journal')}"><circle cx="${cx}" cy="${cy}" data-r="${radius(count.journals, max)}"/></g>`; }).join('');
  scaleBubbles();
  document.querySelectorAll('.bubble').forEach(node => {
    const city = cityById.get(node.dataset.city), count = counts.get(node.dataset.city);
    node.addEventListener('mouseenter', event => showTip(event, city, count, total));
    node.addEventListener('focus', event => showTip(event, city, count, total));
    node.addEventListener('mouseleave', hideTip); node.addEventListener('blur', hideTip);
    node.addEventListener('click', () => { if (!suppressClick) selectCity(city.id); });
    node.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectCity(city.id); } });
  });
  drawLegend(max);
}

// Size legend in the empty Indian Ocean corner; drawn outside the zoom group and scaled like the bubbles.
let legendMax = 1;
function niceStep(value) { const power = 10 ** Math.floor(Math.log10(Math.max(1, value))); return [1, 2, 5, 10].map(f => f * power).reverse().find(v => v <= value) || power; }
function drawLegend(max = legendMax) {
  legendMax = max; const factor = state.view.zoom ** 0.75, size = value => radius(value, max) / factor;
  const values = [...new Set([niceStep(max), niceStep(max / 4), niceStep(max / 25)])].filter(v => v >= 1 && v <= max);
  if (!values.length) { $('#legend').innerHTML = ''; return; }
  const cx = 62, base = 412, big = size(values[0]), labelX = cx + big + 14;
  const rings = values.map(value => { const r = size(value), top = base - 2 * r;
    return `<circle cx="${cx}" cy="${base - r}" r="${r}"/><line x1="${cx}" y1="${top}" x2="${labelX - 4}" y2="${top}"/><text x="${labelX}" y="${top + 4}">${fmt.format(value)}</text>`; }).join('');
  $('#legend').innerHTML = `<text class="legend-title" x="${cx - big}" y="${base - 2 * big - 12}">Journals</text>${rings}`;
}

/* ---------- Provinces & cities ---------- */
function bar(value, max) { return `<span class="bar" aria-hidden="true"><span style="width:${max ? (100 * value / max).toFixed(1) : 0}%"></span></span>`; }

function renderProvinces(journals, counts) {
  const provinces = new Map();
  for (const [id, count] of counts) {
    const city = cityById.get(id); if (!city) continue;
    const name = city.province || 'Province not recorded';
    const row = provinces.get(name) || { name, journals: 0, cities: [] };
    row.journals += count.journals; row.cities.push({ city, journals: count.journals });
    provinces.set(name, row);
  }
  const rows = [...provinces.values()].sort((a, b) => b.journals - a.journals || a.name.localeCompare(b.name));
  const max = rows[0]?.journals || 0, total = journals.length;
  const unmapped = journals.filter(j => j.location.status === 'unmapped').length;
  const open = state.city && cityById.get(state.city)?.province;
  const TOP = 10, openIndex = rows.findIndex(row => row.name === open);
  const shown = state.allProvinces || openIndex >= TOP ? rows : rows.slice(0, TOP);
  $('#province-list').innerHTML = shown.map((row, i) => {
    row.cities.sort((a, b) => b.journals - a.journals || a.city.name.localeCompare(b.city.name));
    const cities = row.cities.map(({ city, journals: n }) => `<li><button type="button" class="rank-link${state.city === city.id ? ' active' : ''}" data-city="${esc(city.id)}"><span>${esc(city.name)}</span><span class="count">${fmt.format(n)}</span></button></li>`).join('');
    return `<details class="rank-item"${open === row.name ? ' open' : ''}><summary><span class="rank">${i + 1}</span><span class="rank-name">${esc(row.name)}<small>${plural(row.cities.length, 'city')}</small></span><span class="count">${fmt.format(row.journals)}</span>${bar(row.journals, max)}</summary><ul class="sub-list">${cities}</ul></details>`;
  }).join('') + (rows.length > TOP && openIndex < TOP ? `<button type="button" class="show-more" id="toggle-provinces">${state.allProvinces ? 'Show top 10 only' : `Show all ${rows.length} provinces`}</button>` : '') + `<div class="rank-item static"><button type="button" class="rank-link muted-row" data-city="${UNMAPPED}"><span class="rank">–</span><span class="rank-name">Location not determined<small>${pct.format(total ? 100 * unmapped / total : 0)}% of journals shown</small></span><span class="count">${fmt.format(unmapped)}</span></button></div>`;
  $('#toggle-provinces')?.addEventListener('click', () => { state.allProvinces = !state.allProvinces; renderProvinces(journals, counts); });
  document.querySelectorAll('#province-list [data-city]').forEach(node => node.addEventListener('click', () => {
    selectCity(node.dataset.city); $('.data-section').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }));
}

/* ---------- Fields of science ---------- */
function renderFields(journals) {
  const domains = new Map();
  for (const journal of journals) {
    const domainName = journal.domain?.name || 'Unclassified';
    const domain = domains.get(domainName) || { name: domainName, journals: 0, fields: new Map() };
    domain.journals += 1;
    const key = fieldKey(journal), field = domain.fields.get(key) || { id: key, name: journal.field?.name || 'No topic data', journals: 0 };
    field.journals += 1; domain.fields.set(key, field); domains.set(domainName, domain);
  }
  const rows = [...domains.values()].sort((a, b) => (a.name === 'Unclassified') - (b.name === 'Unclassified') || b.journals - a.journals);
  const max = Math.max(0, ...rows.map(row => row.journals)), total = journals.length;
  $('#field-list').innerHTML = rows.map(row => {
    const fields = [...row.fields.values()].sort((a, b) => b.journals - a.journals);
    const items = fields.map(field => `<li><button type="button" class="rank-link${state.field === field.id ? ' active' : ''}" data-field="${esc(field.id)}"><span>${esc(field.name)}</span><span class="count">${fmt.format(field.journals)}</span></button></li>`).join('');
    const open = fields.some(field => field.id === state.field);
    return `<details class="rank-item"${open ? ' open' : ''}><summary><span class="rank-name">${esc(row.name)}<small>${pct.format(total ? 100 * row.journals / total : 0)}% of journals${row.name === 'Unclassified' ? ' · no topic data' : ` · ${plural(fields.length, 'field')}`}</small></span><span class="count">${fmt.format(row.journals)}</span>${bar(row.journals, max)}</summary><ul class="sub-list">${items}</ul></details>`;
  }).join('');
  document.querySelectorAll('#field-list [data-field]').forEach(node => node.addEventListener('click', () => {
    selectField(node.dataset.field); $('.data-section').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }));
}

/* ---------- Type of publisher ---------- */
function renderTypes(journals) {
  const counts = new Map();
  for (const journal of journals) counts.set(typeKey(journal), (counts.get(typeKey(journal)) || 0) + 1);
  const total = journals.length, max = Math.max(0, ...[...counts].filter(([key]) => key !== NO_TYPE).map(([, n]) => n));
  $('#type-list').innerHTML = TYPES.filter(([key]) => counts.get(key)).map(([key, label, note]) => {
    const n = counts.get(key);
    return `<div class="rank-item static"><button type="button" class="rank-row${key === NO_TYPE ? ' dim' : ''}${state.ptype === key ? ' active' : ''}" data-type="${key}" aria-pressed="${state.ptype === key}"><span class="rank-name">${label}<small>${pct.format(total ? 100 * n / total : 0)}% · ${note}</small></span><span class="count">${fmt.format(n)}</span>${key === NO_TYPE ? '' : bar(n, max)}</button></div>`;
  }).join('');
  document.querySelectorAll('#type-list [data-type]').forEach(node => node.addEventListener('click', () => {
    selectType(node.dataset.type); $('.data-section').scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }));
}

/* ---------- Table ---------- */
function haystack(journal) {
  const city = cityById.get(journal.location.city_id);
  return [journal.name, journal.openalex_name, journal.issn_l, ...journal.issn, journal.publisher.name, city?.name, city?.province, journal.field?.name]
    .join(' ').toLocaleLowerCase('en');
}
function matches(journal) {
  if (!inMode(journal)) return false;
  if (state.city === UNMAPPED ? journal.location.status !== 'unmapped' : state.city && journal.location.city_id !== state.city) return false;
  if (state.field && fieldKey(journal) !== state.field) return false;
  if (state.ptype && typeKey(journal) !== state.ptype) return false;
  return !state.query || haystack(journal).includes(state.query);
}
function badges(journal) {
  const parts = [];
  if (journal.diamond === true) parts.push('<span class="badge diamond" title="DOAJ: no APC and no other fees">Diamond</span>');
  if (journal.is_oa === true) parts.push('<span class="badge">OA</span>');
  if (journal.is_in_doaj === true) parts.push('<span class="badge">DOAJ</span>');
  return parts.join(' ') || '<span class="muted">—</span>';
}
function titleCell(journal) {
  const link = `<a href="https://openalex.org/${encodeURIComponent(journal.id)}" target="_blank" rel="noopener">${esc(journal.name)}</a>`;
  if (journal.name_source === 'openalex') return link;
  const source = journal.name_source === 'doaj' ? 'DOAJ' : 'Crossref';
  return `${link}<small class="title-note" title="OpenAlex name: ${esc(journal.openalex_name)}">Title per ISSN record (${source})</small>`;
}

function renderTable() {
  const filtered = state.data.journals.filter(matches), pages = Math.max(1, Math.ceil(filtered.length / state.perPage));
  state.page = Math.min(Math.max(1, state.page), pages);
  const rows = filtered.slice((state.page - 1) * state.perPage, state.page * state.perPage);
  $('#journal-rows').innerHTML = rows.length ? rows.map(j => { const city = cityById.get(j.location.city_id);
    return `<tr><td>${titleCell(j)}</td><td>${esc(j.publisher.name || '—')}${j.publisher.type ? `<small class="cell-note">${typeLabel(j.publisher.type)}</small>` : ''}</td><td>${city ? esc(cityLabel(city)) : '<span class="muted">Not determined</span>'}</td><td>${esc(j.field?.name || '—')}</td><td>${esc(j.issn_l || '—')}</td><td>${fmt.format(j.works_count)}</td><td>${badges(j)}</td></tr>`; }).join('')
    : '<tr><td colspan="7" class="empty">No journals match these filters. <button type="button" class="link-button" id="empty-reset">Clear filters</button></td></tr>';
  $('#empty-reset')?.addEventListener('click', clearFilters);
  // The heading names the active filter (e.g. "City: Padang"); the mode is reported in the result count.
  const parts = [];
  if (state.city) parts.push(state.city === UNMAPPED ? 'Location not determined' : `City: ${cityById.get(state.city)?.name || state.city}`);
  if (state.field) parts.push(`Field: ${fieldName(state.field)}`);
  if (state.ptype) parts.push(`Publisher type: ${typeLabel(state.ptype)}`);
  $('#table-title').textContent = parts.length ? parts.join(' · ') : MODES[state.mode].title;
  $('#result-count').textContent = `${plural(filtered.length, MODES[state.mode].noun)} found`;
  $('#page-status').textContent = `Page ${fmt.format(state.page)} of ${fmt.format(pages)}`;
  $('#previous').disabled = state.page <= 1; $('#next').disabled = state.page >= pages;
}
function fieldName(id) {
  if (id === UNCLASSIFIED) return 'No topic data';
  return state.data.journals.find(j => j.field?.id === id)?.field.name || id;
}

/* ---------- State ---------- */
function writeUrl() {
  const params = new URLSearchParams();
  if (state.mode !== 'all') params.set('mode', state.mode);
  if (state.city) params.set('city', state.city);
  if (state.field) params.set('field', state.field);
  if (state.ptype) params.set('publisher', state.ptype);
  if (state.query) params.set('q', state.query);
  const query = params.toString();
  history.replaceState(null, '', query ? `?${query}` : location.pathname);
}
function readUrl() {
  const params = new URLSearchParams(location.search);
  state.mode = MODES[params.get('mode')] ? params.get('mode') : 'all';
  state.city = params.get('city') || ''; state.field = params.get('field') || '';
  state.ptype = TYPES.some(([id]) => id === params.get('publisher')) ? params.get('publisher') : '';
  state.query = (params.get('q') || '').toLocaleLowerCase('en');
}

function render() {
  const journals = modeJournals(), counts = cityCounts(journals);
  document.querySelectorAll('.segmented button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mode === state.mode)));
  $('#mode-note').hidden = state.mode === 'all'; $('#mode-note').innerHTML = MODES[state.mode].note;
  $('#city-filter').value = state.city; $('#field-filter').value = state.field; $('#type-filter').value = state.ptype; $('#search').value = state.query;
  renderKpis(journals, counts); drawBubbles(counts, journals.length || 1);
  renderProvinces(journals, counts); renderFields(journals); renderTypes(journals); renderTable(); writeUrl();
}
function selectCity(id) { state.city = state.city === id ? '' : id; state.page = 1; render(); }
function selectField(id) { state.field = state.field === id ? '' : id; state.page = 1; render(); }
function selectType(id) { state.ptype = state.ptype === id ? '' : id; state.page = 1; render(); }
function setMode(mode) { state.mode = mode; state.page = 1; render(); }
function clearFilters() { Object.assign(state, { city: '', field: '', ptype: '', query: '', page: 1 }); render(); }

/* ---------- Zoom and pan ---------- */
// The viewport group is drawn at translate(x, y) scale(zoom) in SVG units; it is clamped so the map always fills the frame.
const MAP_W = 1000, MAP_H = 430, MAX_ZOOM = 8;
let suppressClick = false;
// Bubbles grow only gently with zoom, so nearby cities separate instead of covering each other.
function scaleBubbles() {
  const factor = state.view.zoom ** 0.75;
  document.querySelectorAll('#bubbles circle').forEach(circle => circle.setAttribute('r', (circle.dataset.r / factor).toFixed(2)));
  drawLegend();
}
function applyView() {
  const v = state.view;
  v.x = Math.min(0, Math.max(MAP_W * (1 - v.zoom), v.x)); v.y = Math.min(0, Math.max(MAP_H * (1 - v.zoom), v.y));
  $('#viewport').setAttribute('transform', `translate(${v.x.toFixed(2)} ${v.y.toFixed(2)}) scale(${v.zoom})`);
  $('#map-wrap').classList.toggle('zoomed', v.zoom > 1);
  scaleBubbles();
  $('#zoom-out').disabled = v.zoom <= 1; $('#zoom-in').disabled = v.zoom >= MAX_ZOOM;
}
// Zoom so the SVG point (cx, cy) stays where it is on screen; the default is the centre of what is visible.
function zoomTo(value, cx = MAP_W / 2, cy = MAP_H / 2) {
  const v = state.view, zoom = Math.max(1, Math.min(MAX_ZOOM, value));
  v.x = cx - (cx - v.x) / v.zoom * zoom; v.y = cy - (cy - v.y) / v.zoom * zoom; v.zoom = zoom;
  hideTip(); applyView();
}
function panBy(dx, dy) { state.view.x += dx; state.view.y += dy; hideTip(); applyView(); }
function svgPoint(event) {
  const rect = $('#map').getBoundingClientRect();
  return [(event.clientX - rect.left) * MAP_W / rect.width, (event.clientY - rect.top) * MAP_H / rect.height];
}
function bindPanZoom() {
  const svg = $('#map'), pointers = new Map();
  let last = null, pinch = null, moved = 0;
  svg.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    pointers.set(event.pointerId, svgPoint(event)); moved = 0; suppressClick = false;
    if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { distance: Math.hypot(a[0] - b[0], a[1] - b[1]), zoom: state.view.zoom }; }
    last = svgPoint(event);
  });
  svg.addEventListener('pointermove', event => {
    if (!pointers.has(event.pointerId)) return;
    const point = svgPoint(event); pointers.set(event.pointerId, point);
    if (pointers.size === 2 && pinch) {
      const [a, b] = [...pointers.values()];
      zoomTo(pinch.zoom * Math.hypot(a[0] - b[0], a[1] - b[1]) / (pinch.distance || 1), (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
      suppressClick = true; return;
    }
    if (state.view.zoom <= 1 || !last) return;
    moved += Math.abs(point[0] - last[0]) + Math.abs(point[1] - last[1]);
    if (moved > 6 && !suppressClick) { suppressClick = true; svg.setPointerCapture(event.pointerId); svg.classList.add('dragging'); }
    if (suppressClick) panBy(point[0] - last[0], point[1] - last[1]);
    last = point;
  });
  const end = event => {
    pointers.delete(event.pointerId); if (pointers.size < 2) pinch = null;
    last = pointers.size ? [...pointers.values()][0] : null; svg.classList.remove('dragging');
  };
  svg.addEventListener('pointerup', end); svg.addEventListener('pointercancel', end);
  // Trackpad pinch arrives as ctrl+wheel; a plain wheel keeps scrolling the page.
  svg.addEventListener('wheel', event => {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault(); const [cx, cy] = svgPoint(event);
    zoomTo(state.view.zoom * Math.exp(-event.deltaY * 0.01), cx, cy);
  }, { passive: false });
  svg.addEventListener('keydown', event => {
    const step = 60, keys = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (keys[event.key] && state.view.zoom > 1) { event.preventDefault(); panBy(...keys[event.key]); }
    else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomTo(state.view.zoom * 1.5); }
    else if (event.key === '-') { event.preventDefault(); zoomTo(state.view.zoom / 1.5); }
  });
}

function fillSelects() {
  $('#city-filter').insertAdjacentHTML('beforeend', [...state.data.cities].sort((a, b) => a.name.localeCompare(b.name))
    .map(city => `<option value="${esc(city.id)}">${esc(cityLabel(city))}</option>`).join(''));
  const fields = new Map();
  for (const journal of state.data.journals) if (journal.field) fields.set(journal.field.id, journal.field.name);
  $('#field-filter').insertAdjacentHTML('beforeend', [...fields].sort((a, b) => a[1].localeCompare(b[1]))
    .map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`).join('') + `<option value="${UNCLASSIFIED}">No topic data</option>`);
  $('#type-filter').insertAdjacentHTML('beforeend', TYPES.map(([id, label]) => `<option value="${id}">${label}</option>`).join(''));
}

function bind() {
  let timer;
  $('#search').addEventListener('input', event => { clearTimeout(timer); timer = setTimeout(() => { state.query = event.target.value.trim().toLocaleLowerCase('en'); state.page = 1; renderTable(); writeUrl(); }, 150); });
  $('#city-filter').addEventListener('change', event => { state.city = event.target.value; state.page = 1; render(); });
  $('#field-filter').addEventListener('change', event => { state.field = event.target.value; state.page = 1; render(); });
  $('#type-filter').addEventListener('change', event => { state.ptype = event.target.value; state.page = 1; render(); });
  $('#show-unmapped').addEventListener('click', () => { state.city = UNMAPPED; state.page = 1; render(); });
  $('#clear-filters').addEventListener('click', clearFilters);
  document.querySelectorAll('.segmented button').forEach(button => button.addEventListener('click', () => setMode(button.dataset.mode)));
  $('#previous').addEventListener('click', () => { state.page--; renderTable(); });
  $('#next').addEventListener('click', () => { state.page++; renderTable(); });
  $('#zoom-in').addEventListener('click', () => zoomTo(state.view.zoom * 1.5));
  $('#zoom-out').addEventListener('click', () => zoomTo(state.view.zoom / 1.5));
  $('#zoom-reset').addEventListener('click', () => { Object.assign(state.view, { zoom: 1, x: 0, y: 0 }); applyView(); clearFilters(); });
  bindPanZoom(); applyView();
}

async function start() {
  $('#kpis').innerHTML = '<div class="kpi skeleton"></div>'.repeat(6);
  fetch('assets/basemap.json').then(response => response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))
    .then(drawLand).catch(error => console.error('Basemap load failed:', error.message));
  try {
    const response = await fetch('data/journals-map.json', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    cityById = new Map(state.data.cities.map(city => [city.id, city]));
    $('#updated').textContent = `Updated ${new Intl.DateTimeFormat('en-GB', { dateStyle: 'long', timeZone: 'Asia/Jakarta' }).format(new Date(state.data.generated_at))}`;
    readUrl(); fillSelects(); bind(); render();
  } catch (error) {
    $('#kpis').innerHTML = ''; $('#updated').textContent = 'Data not built yet';
    const status = $('#status'); status.hidden = false;
    status.textContent = 'The data snapshot could not be loaded. Run the data pipeline (collect, registry, geocode, build) and reload this page.';
    console.error('Snapshot load failed:', error.message);
  }
}
start();
