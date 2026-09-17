/* Vues : Recherche, Résultats d'une recherche, Suivi. */
import { $, $$, esc, api, state, toast, fmtDate, sectorLabel, monogram, pageHead, enableTilt,
         STATUS, CATEGORY, CAT_FAMILY, SECTOR_FAMILY, FAMILY_INK, HEADCOUNT } from './core.js';

/* ======================= recherche ======================= */
export async function renderSearch() {
  $('#view').innerHTML = `
    ${pageHead({ kicker: 'Trouver à qui écrire', title: 'Nouvelle recherche',
                 sub: 'Décris le poste et la cible ; l’outil remonte les personnes à qui écrire, et pourquoi elles. Pour envoyer, passe plutôt par une <a href="#/campagnes/nouvelle" class="accent">campagne</a>.' })}
    <section class="card"><div class="card-body" id="formBody"></div></section>
    <section class="section">
      <div class="section-head"><h2><span class="emoji">🗂️</span>Recherches récentes</h2></div>
      <div class="list runs" id="runs"><div class="empty">Chargement…</div></div>
    </section>`;
  renderForm();
  loadRuns();
}

export function renderForm() {
  const f = state.form, body = $('#formBody');
  if (!body) return;
  if (state.job && !state.job.done) return renderProgress(body);

  body.innerHTML = `
    <div class="field" style="margin-bottom:22px">
      <label class="lbl" for="job">Poste visé</label>
      <input class="input input-hero" id="job" placeholder="ex. développeur Python, product designer, chef de projet…" value="${esc(f.job)}" autocomplete="off">
      <div class="hint">Sert à repérer les interlocuteurs dont le métier recoupe le tien. <span class="kbd">/</span> pour y revenir.</div>
    </div>
    <div class="grid-3" style="margin-bottom:22px">
      <div class="field">
        <label class="lbl" for="zone">Zone</label>
        <input class="input" id="zone" placeholder="ex. 33 ou 33000" value="${esc(f.zone)}" autocomplete="off">
        <div class="hint" id="zoneHint">Département ou code postal. Vide = toute la France.</div>
      </div>
      <div class="field">
        <span class="lbl">Taille d’entreprise</span>
        <div class="pills" id="head">${HEADCOUNT.map(h => `<button type="button" class="pill ${f.head === h.key ? 'on' : ''}" data-k="${h.key}">${h.label}</button>`).join('')}</div>
        <div class="hint" id="headHint"></div>
      </div>
      <div class="field">
        <label class="lbl" for="limit">Entreprises</label>
        <select class="input" id="limit">${[10, 25, 50, 100].map(n => `<option ${f.limit === n ? 'selected' : ''}>${n}</option>`).join('')}</select>
        <div class="hint">À explorer par recherche.</div>
      </div>
    </div>
    <div class="field" style="margin-bottom:20px">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <span class="lbl">Secteurs</span><span class="hint" id="secHint"></span><span style="flex:1"></span>
        <input class="input" id="secFilter" placeholder="Filtrer…" style="width:160px;padding:7px 12px;font-size:13px">
      </div>
      <div class="pills" id="chips"></div>
    </div>
    <details class="adv">
      <summary>Options avancées</summary>
      <div class="adv-body">
        <div class="field">
          <label class="lbl" for="kw">Mot dans le nom de l’entreprise</label>
          <input class="input" id="kw" placeholder="ex. studio, lab…" value="${esc(f.keywords)}">
          <div class="hint">Cherche dans la raison sociale, pas dans l’activité.</div>
        </div>
        <div style="display:grid;gap:10px;align-content:end">
          <label class="check"><input type="checkbox" id="useSearch" ${f.useSearch ? 'checked' : ''}> Moteur de recherche en repli si le site n’est pas déduit</label>
          <label class="check"><input type="checkbox" id="smtp" ${f.smtp ? 'checked' : ''}> Sonder les adresses déduites en SMTP (lent, peu fiable)</label>
        </div>
      </div>
    </details>
    <div style="display:flex;align-items:center;gap:14px;margin-top:26px;flex-wrap:wrap">
      <button class="btn btn-primary" id="go">Lancer la recherche</button>
      <span class="hint" id="goHint"></span>
    </div>`;

  $('#job').oninput = e => { f.job = e.target.value; $('#job').classList.remove('err'); };
  $('#zone').oninput = e => { f.zone = e.target.value; $('#zone').classList.remove('err'); $('#zoneHint').className = 'hint'; $('#zoneHint').textContent = 'Département ou code postal. Vide = toute la France.'; };
  $('#limit').onchange = e => f.limit = +e.target.value;
  $('#kw').oninput = e => f.keywords = e.target.value;
  $('#useSearch').onchange = e => f.useSearch = e.target.checked;
  $('#smtp').onchange = e => f.smtp = e.target.checked;
  $$('#head .pill').forEach(b => b.onclick = () => { f.head = b.dataset.k; renderForm(); });
  $('#secFilter').oninput = e => renderChips(e.target.value);
  $('#go').onclick = () => launch();
  $('#job').addEventListener('keydown', e => { if (e.key === 'Enter') launch(); });
  renderHeadHint();
  renderChips('');
}

function renderHeadHint() {
  const f = state.form, el = $('#headHint'); if (!el) return;
  if (f.head === 'custom') {
    el.innerHTML = `<span style="display:inline-flex;gap:8px;align-items:center;margin-top:4px">
      <input class="input" id="hmin" placeholder="min" style="width:76px;padding:6px 10px" value="${esc(f.min)}"> –
      <input class="input" id="hmax" placeholder="max" style="width:76px;padding:6px 10px" value="${esc(f.max)}"> salariés</span>`;
    $('#hmin').oninput = e => f.min = e.target.value;
    $('#hmax').oninput = e => f.max = e.target.value;
  } else if (f.head === 'pme') el.innerHTML = '<b>Le meilleur rendement</b> : assez grand pour recruter, assez petit pour lire ses mails.';
  else if (f.head === 'eti') el.textContent = 'Au-delà de 250, les adresses sont presque toujours derrière un formulaire.';
  else el.textContent = '';
}

function renderChips(filter) {
  const f = state.form, q = filter.trim().toLowerCase();
  $('#chips').innerHTML = state.sectors.map(s => {
    const on = f.sectors.has(s.key);
    const dim = q && !s.label.toLowerCase().includes(q) && !on;
    const ink = FAMILY_INK[SECTOR_FAMILY[s.key]] || 'var(--muted)';
    return `<button type="button" class="pill ${on ? 'on' : ''} ${dim ? 'dim' : ''}" data-k="${s.key}"><i style="background:${on ? '#fff' : ink}"></i>${esc(s.label.split(' / ')[0])}</button>`;
  }).join('');
  $$('#chips .pill').forEach(c => c.onclick = () => {
    const k = c.dataset.k; f.sectors.has(k) ? f.sectors.delete(k) : f.sectors.add(k);
    renderChips($('#secFilter').value);
  });
  const n = f.sectors.size;
  $('#secHint').textContent = n ? `${n} sélectionné${n > 1 ? 's' : ''}` : 'Choisis-en au moins un.';
}

export function parseZone(raw) {
  const z = (raw || '').trim().toUpperCase();
  if (!z) return {};
  if (/^\d{5}$/.test(z)) return { postal_code: z };
  if (/^(\d{2,3}|2A|2B)$/.test(z)) return { department: z };
  return null;
}

export function headcountRange(head, min, max) {
  if (head === 'custom') return { min_headcount: min ? +min : null, max_headcount: max ? +max : null };
  const h = HEADCOUNT.find(x => x.key === head) || HEADCOUNT[0];
  return { min_headcount: h.min, max_headcount: h.max };
}

/* Lance une recherche. `onDone(runId)` permet à l'assistant de campagne de
   réutiliser le même flux sans passer par la page Recherche. */
export async function startSearch(payload, onDone) {
  const { run_id } = await api('/api/search', { method: 'POST', body: JSON.stringify(payload) });
  state.job = { runId: run_id, log: [], processed: 0, total: 0, step: 'Interrogation de l’annuaire des entreprises…', done: false, onDone };
  listen(run_id);
  return run_id;
}

async function launch() {
  const f = state.form; let ok = true;
  if (!f.job.trim()) { $('#job').classList.add('err'); $('#job').focus(); ok = false; }
  const zone = parseZone(f.zone);
  if (zone === null) { $('#zone').classList.add('err'); $('#zoneHint').className = 'hint err'; $('#zoneHint').textContent = 'Un département (33) ou un code postal (33000).'; ok = false; }
  if (!f.sectors.size) { $('#secHint').innerHTML = '<span class="hint err">Choisis au moins un secteur.</span>'; ok = false; }
  if (!ok) return;
  const payload = { job_title: f.job.trim(), sectors: [...f.sectors], keywords: f.keywords, ...zone, limit: f.limit,
                    use_search_engine: f.useSearch, verify_smtp: f.smtp, ...headcountRange(f.head, f.min, f.max) };
  $('#go').disabled = true;
  try {
    await startSearch(payload, (runId) => { location.hash = `#/run/${runId}`; });
    renderForm();
  } catch (e) { $('#go').disabled = false; $('#goHint').className = 'hint err'; $('#goHint').textContent = e.message; }
}

function listen(runId) {
  const es = new EventSource(`/api/runs/${runId}/stream`);
  es.onmessage = (msg) => {
    const e = JSON.parse(msg.data), j = state.job;
    if (!j || j.runId !== runId) return;
    if (e.total) { j.total = e.total; j.processed = e.processed || 0; }
    if (e.event === 'etape') { j.step = e.step; j.log.push('> ' + e.step); }
    else if (e.event === 'entreprise') { j.step = `${e.name} — ${e.step}`; j.log.push(`  ${e.name} : ${e.step}`); }
    else if (e.event === 'erreur') j.log.push(`  ! ${e.name} : ${e.detail}`);
    else if (e.event === 'erreur_fatale') { j.step = 'Échec : ' + e.detail; j.log.push('ÉCHEC ' + e.detail); j.done = true; }
    else if (e.event === 'termine') { j.step = `${e.total_contacts} contact(s) retenus`; j.done = true; j.processed = j.total; }
    else if (e.event === 'fin_flux') { es.close(); if (j.done) setTimeout(() => { const cb = j.onDone; state.job = null; cb && cb(runId); }, 700); }
    updateProgress();
  };
  es.onerror = () => { es.close(); if (state.job) { state.job.done = true; updateProgress(); } };
}

export function renderProgress(body) {
  body.innerHTML = `
    <div class="progress">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">
        <h2 id="pTitle">Recherche en cours</h2><span class="muted" id="pCount"></span>
      </div>
      <div class="bar"><i id="pBar"></i></div>
      <div class="step"><span class="pulse" id="pPulse"></span><span id="pStep"></span></div>
      <details class="adv"><summary>Journal</summary><div class="journal" id="pLog"></div></details>
    </div>`;
  updateProgress();
}
export function updateProgress() {
  const j = state.job; if (!j || !$('#pStep')) return;
  $('#pStep').textContent = j.step;
  $('#pCount').textContent = j.total ? `${j.processed} / ${j.total} entreprises` : '';
  $('#pBar').style.width = j.total ? `${Math.round(100 * j.processed / j.total)}%` : '4%';
  const log = $('#pLog'); if (log) { log.textContent = j.log.slice(-200).join('\n'); log.scrollTop = log.scrollHeight; }
  if (j.done) { $('#pTitle').textContent = 'Terminé'; $('#pPulse').style.animation = 'none'; }
}

async function loadRuns() {
  const el = $('#runs'); if (!el) return;
  let runs = [];
  try { runs = await api('/api/runs'); } catch { el.innerHTML = '<div class="empty">Historique indisponible.</div>'; return; }
  runs = runs.filter(r => r.status !== 'en cours' || !state.job || r.id !== state.job.runId);
  if (!runs.length) { el.innerHTML = '<div class="empty"><b>Aucune recherche pour l’instant</b>Ta première recherche apparaîtra ici.</div>'; return; }
  el.innerHTML = runs.map(r => {
    const secs = (r.sectors || '').split(',').filter(Boolean).map(sectorLabel);
    const fam = SECTOR_FAMILY[(r.sectors || '').split(',')[0]] || '';
    const secTxt = secs.length > 2 ? `${secs.slice(0, 2).join(', ')} +${secs.length - 2}` : secs.join(', ');
    const zone = r.department ? `dép. ${r.department}` : '';
    const day = r.started_at ? new Date(r.started_at).getDate() : '·';
    const st = r.status === 'echec' ? '<span class="tag danger">échec</span>' : r.status === 'en cours' ? '<span class="tag">en cours</span>' : '';
    return `<div class="item clickable" data-id="${r.id}">
      <div class="mono ${fam}">${day}</div>
      <div><div class="t">${esc(r.job_title)}${st}</div><div class="s">${esc([secTxt, zone, fmtDate(r.started_at)].filter(Boolean).join(' · '))}</div></div>
      <div class="meta"><b>${r.contacts ?? 0} contact${(r.contacts ?? 0) > 1 ? 's' : ''}</b>${r.companies ?? 0} entreprises</div>
    </div>`;
  }).join('');
  $$('#runs .item').forEach(r => r.onclick = () => location.hash = `#/run/${r.dataset.id}`);
  enableTilt(el, 4);
}

/* ======================= résultats ======================= */
export async function renderRun(id) {
  const view = $('#view');
  view.innerHTML = '<div class="empty">Chargement…</div>';
  let run, rows;
  try { [run, rows] = await Promise.all([api(`/api/runs/${id}`), api(`/api/runs/${id}/contacts`)]); }
  catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }

  const secs = (run.sectors || '').split(',').filter(Boolean).map(sectorLabel).join(', ');
  const zone = run.department ? `dép. ${run.department}` : '';
  const r = state.results; r.open = new Set(); r.dismissedBanner = false;

  view.innerHTML = `
    ${pageHead({ back: { href: '#/recherche', label: 'Recherches' },
                 kicker: esc([secs, zone, fmtDate(run.started_at)].filter(Boolean).join(' · ')), title: esc(run.job_title),
                 sub: `${run.companies ?? 0} entreprises explorées, ${rows.length} contact${rows.length > 1 ? 's' : ''} trouvé${rows.length > 1 ? 's' : ''}. Clique une ligne pour lire pourquoi elle est là.`,
                 right: `<a class="btn btn-white" href="/api/runs/${id}/export">Exporter CSV</a><a class="btn btn-accent" href="#/campagnes/nouvelle?run=${id}">Créer une campagne</a>` })}
    <div class="toolbar">
      <div class="pills on-paper" id="catSeg">
        ${[['all', 'Tous'], ['rh', 'RH'], ['direction', 'Direction'], ['nominatif', 'Nominatifs'], ['generique', 'Génériques']]
          .map(([k, l]) => `<button class="pill ${r.filter === k ? 'on' : ''}" data-k="${k}">${k !== 'all' && CAT_FAMILY[k] ? `<i style="background:${r.filter === k ? '#fff' : FAMILY_INK[CAT_FAMILY[k]]}"></i>` : ''}${l}</button>`).join('')}
      </div>
      <label class="check"><input type="checkbox" id="hideLow" ${r.hideLow ? 'checked' : ''}> Masquer les peu pertinents</label>
      <span class="grow"></span>
      <input class="input" id="q" placeholder="Filtrer…">
    </div>
    <div id="banner"></div>
    <div class="list results" id="rows"></div>`;

  $$('#catSeg .pill').forEach(b => b.onclick = () => { r.filter = b.dataset.k; renderRun(id); });
  $('#hideLow').onchange = e => { r.hideLow = e.target.checked; drawRows(rows); };
  $('#q').oninput = e => { r.q = e.target.value.toLowerCase(); drawRows(rows); };
  drawRows(rows);
  enableTilt($('#rows'), 3);
}

function drawRows(all) {
  const r = state.results;
  const visible = all.filter(c =>
    (r.filter === 'all' || c.category === r.filter || (r.filter === 'nominatif' && c.is_nominative && c.category !== 'rh' && c.category !== 'direction')) &&
    (!r.hideLow || c.score >= 40) &&
    (!r.q || `${c.email} ${c.company_name} ${c.first_name || ''} ${c.last_name || ''} ${c.role_title || ''}`.toLowerCase().includes(r.q)));

  const inferred = visible.filter(c => c.inferred).length;
  $('#banner').innerHTML = inferred && !r.dismissedBanner ? `
    <div class="banner"><span><b>${inferred} adresse${inferred > 1 ? 's' : ''} déduite${inferred > 1 ? 's' : ''}</b> du motif d’adressage de l’entreprise — jamais vue${inferred > 1 ? 's' : ''} en ligne.
    Vérifie-les (LinkedIn, recherche du nom) avant d’écrire.</span><button class="x" title="Masquer">×</button></div>` : '';
  const x = $('#banner .x'); if (x) x.onclick = () => { r.dismissedBanner = true; $('#banner').innerHTML = ''; };

  const el = $('#rows');
  if (!visible.length) {
    const hidden = all.length - visible.length;
    el.innerHTML = `<div class="empty"><b>Aucun contact${hidden ? ' visible' : ''}</b>${hidden ? `${hidden} masqué${hidden > 1 ? 's' : ''} par les filtres.` : 'Élargis le secteur, la zone ou la taille.'}</div>`;
    return;
  }
  el.innerHTML = visible.map(c => {
    const tier = c.score >= 80 ? '' : c.score >= 50 ? 'mid' : 'low';
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ');
    const flags = [
      c.inferred ? '<span class="tag" title="Reconstituée depuis le motif maison, jamais vue en ligne">déduite</span>' : '',
      c.mx_ok === 0 ? '<span class="tag danger" title="Aucun serveur de messagerie déclaré">sans MX</span>' : '',
    ].join('');
    const st = c.outreach_status;
    const act = st
      ? `<select class="status ${st}" data-email="${esc(c.email)}">${Object.entries(STATUS).map(([k, l]) => `<option value="${k}" ${k === st ? 'selected' : ''}>${l}</option>`).join('')}</select>`
      : `<button class="btn btn-sm btn-primary" data-follow="${esc(c.email)}">Suivre</button>
         <button class="btn btn-sm btn-text" data-drop="${esc(c.email)}" title="Ne plus proposer">Écarter</button>`;
    const open = r.open.has(c.email);
    const reasons = (c.reasons || '').split(' | ').filter(Boolean);
    const src = c.source_url && c.source_url.startsWith('http') ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">page source ↗</a>` : esc(c.source_url || '');
    return `<div class="item clickable ${open ? 'open' : ''}" data-row="${esc(c.email)}">
      <div class="mono ${CAT_FAMILY[c.category] || ''}" title="${esc(c.company_name || '')}">${esc(monogram(c.company_name))}</div>
      <div style="min-width:0"><div class="t">${esc(c.email)}${flags}</div>
        ${name || c.role_title ? `<div class="s">${name ? `<b>${esc(name)}</b>` : ''}${name && c.role_title ? ' — ' : ''}${esc((c.role_title || '').slice(0, 80))}</div>` : ''}</div>
      <div class="firm meta" style="text-align:left"><b>${esc(c.company_name || '')}</b>${esc([c.city, c.size].filter(Boolean).join(' · '))}</div>
      <div class="catcell"><span class="catpill ${esc(c.category)}"><i></i>${esc(CATEGORY[c.category] || c.category)}</span></div>
      <div class="actions"><span class="score ${tier}">${Math.round(c.score)}</span>${act}</div>
      ${open ? `<div class="detail"><ul>${reasons.map(x => `<li>${esc(x)}</li>`).join('') || '<li>Score de base de la catégorie.</li>'}</ul><div>${src}</div></div>` : ''}
    </div>`;
  }).join('');

  $$('#rows .item').forEach(row => row.onclick = (e) => {
    if (e.target.closest('button, select, a')) return;
    const k = row.dataset.row; r.open.has(k) ? r.open.delete(k) : r.open.add(k); drawRows(all);
  });
  $$('[data-follow]').forEach(b => b.onclick = () => follow(all, b.dataset.follow, 'a_contacter'));
  $$('[data-drop]').forEach(b => b.onclick = () => follow(all, b.dataset.drop, 'ecarte'));
  $$('#rows select.status').forEach(s => s.onchange = async () => {
    await api(`/api/outreach/${encodeURIComponent(s.dataset.email)}`, { method: 'PATCH', body: JSON.stringify({ status: s.value }) });
    const c = all.find(x => x.email === s.dataset.email); if (c) c.outreach_status = s.value;
    s.className = `status ${s.value}`; toast(STATUS[s.value]); window.refreshBadges && window.refreshBadges();
  });
}

async function follow(all, email, status) {
  const c = all.find(x => x.email === email); if (!c) return;
  await api('/api/outreach', { method: 'POST', body: JSON.stringify({
    email, status, company_name: c.company_name, first_name: c.first_name, last_name: c.last_name,
    role_title: c.role_title, category: c.category, score: c.score, run_id: c.run_id }) });
  c.outreach_status = status;
  toast(status === 'ecarte' ? 'Écarté' : 'Ajouté au suivi');
  drawRows(all); window.refreshBadges && window.refreshBadges();
}

/* ======================= suivi ======================= */
export async function renderSuivi() {
  const view = $('#view');
  view.innerHTML = '<div class="empty">Chargement…</div>';
  let rows = [];
  try { rows = await api('/api/outreach'); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const s = state.suivi;
  const counts = Object.fromEntries(Object.keys(STATUS).map(k => [k, rows.filter(r => r.status === k).length]));
  const active = rows.filter(r => r.status !== 'ecarte').length;

  view.innerHTML = `
    ${pageHead({ kicker: `${active} personne${active > 1 ? 's' : ''} en cours`, title: 'Suivi',
                 sub: 'Les personnes que tu suis à la main, et où tu en es avec chacune. Les envois automatiques ont leur propre suivi dans chaque campagne.',
                 right: `<a class="btn btn-white" href="/api/outreach/export">Exporter CSV</a>` })}
    <div class="toolbar">
      <div class="pills on-paper" id="stSeg">
        ${Object.entries(STATUS).map(([k, l]) => `<button class="pill ${s.filter === k ? 'on' : ''}" data-k="${k}">${l} <span class="n">${counts[k]}</span></button>`).join('')}
      </div>
    </div>
    <div class="list suivi" id="srows"></div>`;
  $$('#stSeg .pill').forEach(b => b.onclick = () => { s.filter = b.dataset.k; renderSuivi(); });
  drawSuivi(rows);
  enableTilt($('#srows'), 3);
}

function drawSuivi(rows) {
  const s = state.suivi, el = $('#srows');
  const vis = rows.filter(r => r.status === s.filter);
  if (!vis.length) {
    const msg = s.filter === 'a_contacter'
      ? 'Depuis une recherche, clique <b style="display:inline;font:inherit;color:var(--ink)">Suivre</b> sur un contact pour le retrouver ici.'
      : `Aucun contact au statut « ${STATUS[s.filter]} ».`;
    el.innerHTML = `<div class="empty"><b>Rien ici</b>${msg}</div>`; return;
  }
  el.innerHTML = vis.map(r => {
    const name = [r.first_name, r.last_name].filter(Boolean).join(' ');
    return `<div class="item" data-email="${esc(r.email)}">
      <div class="mono ${CAT_FAMILY[r.category] || ''}" title="${esc(r.company_name || '')}">${esc(monogram(r.company_name))}</div>
      <div style="min-width:0"><div class="t">${esc(r.email)}</div>
        ${name || r.role_title ? `<div class="s">${name ? `<b>${esc(name)}</b>` : ''}${name && r.role_title ? ' — ' : ''}${esc((r.role_title || '').slice(0, 70))}</div>` : ''}</div>
      <div class="meta" style="text-align:left"><b>${esc(r.company_name || '')}</b>${r.run_id ? `<a href="#/run/${r.run_id}" class="accent">recherche #${r.run_id}</a> · ` : ''}${fmtDate(r.updated_at)}</div>
      <select class="status ${r.status}">${Object.entries(STATUS).map(([k, l]) => `<option value="${k}" ${k === r.status ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <div class="note ${r.note ? '' : 'empty-note'}" title="Cliquer pour éditer">${esc(r.note) || 'Ajouter une note…'}</div>
      <div class="actions"><button class="btn btn-sm btn-text" data-del title="Retirer du suivi">×</button></div>
    </div>`;
  }).join('');

  $$('#srows .item').forEach(row => {
    const email = row.dataset.email;
    const patch = (body) => api(`/api/outreach/${encodeURIComponent(email)}`, { method: 'PATCH', body: JSON.stringify(body) });
    $('select', row).onchange = async (e) => {
      await patch({ status: e.target.value }); rows.find(x => x.email === email).status = e.target.value;
      toast(STATUS[e.target.value]); renderSuivi(); window.refreshBadges && window.refreshBadges();
    };
    $('.note', row).onclick = () => {
      const cur = rows.find(x => x.email === email).note || '';
      $('.note', row).outerHTML = `<input class="input note-input" value="${esc(cur)}" placeholder="Note…" style="padding:6px 10px">`;
      const inp = $('.note-input', row); inp.focus();
      const save = async () => { await patch({ note: inp.value }); rows.find(x => x.email === email).note = inp.value; drawSuivi(rows); };
      inp.onblur = save; inp.onkeydown = e => { if (e.key === 'Enter') inp.blur(); if (e.key === 'Escape') { inp.onblur = null; drawSuivi(rows); } };
    };
    $('[data-del]', row).onclick = async () => {
      if (!confirm(`Retirer ${email} du suivi ?`)) return;
      await api(`/api/outreach/${encodeURIComponent(email)}`, { method: 'DELETE' });
      rows.splice(rows.findIndex(x => x.email === email), 1); toast('Retiré'); drawSuivi(rows); window.refreshBadges && window.refreshBadges();
    };
  });
}
