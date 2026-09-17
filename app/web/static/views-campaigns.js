/* Vues : tableau de bord des campagnes, assistant de création, page de campagne. */
import { $, $$, esc, api, upload, state, toast, fmtDate, fmtDay, relTime, sectorLabel, monogram, pretty, pageHead,
         enableTilt, highlightVars, openModal, closeModal, linkedinSearch, queryParam, contactTags,
         cityPicker, describeCities,
         CATEGORY, CAT_FAMILY, APP_STATUS, CAMP_STATUS, HEADCOUNT } from './core.js';
import { startSearch, headcountRange, renderProgress, updateProgress } from './views-search.js';

const statCard = (k, v, d, tone = '') => `<div class="stat ${tone}"><div class="k">${k}</div><div class="v">${v}</div>${d ? `<div class="d">${d}</div>` : ''}</div>`;
const pct = (a, b) => b ? Math.round(100 * a / b) : 0;

function eventLine(e) {
  const who = e.company_name ? pretty(e.company_name) : '';
  const texts = {
    envoi: `<b>${esc(who)}</b> a reçu votre candidature`,
    ouverture: `<b>${esc(who)}</b> a ouvert votre mail`,
    reponse: `<b>${esc(who)}</b> vous a répondu`,
    erreur: `Erreur — ${esc(e.detail || '')}`,
    lancement: `Campagne lancée — ${esc(e.detail || '')}`,
    pause: 'Campagne mise en pause', reprise: `Campagne reprise — ${esc(e.detail || '')}`,
    creation: `Campagne créée — ${esc(e.detail || '')}`, fin: 'Tous les envois sont partis',
  };
  const kind = { envoi: 'envoi', ouverture: 'ouverture', reponse: 'réponse', erreur: 'erreur' }[e.kind] || e.kind;
  return `<div class="ev ${esc(e.kind)}"><i></i><div><div>${texts[e.kind] || esc(e.detail || e.kind)}</div>
    <div class="when"><span class="kind">${esc(kind)}</span>${relTime(e.at)}</div></div></div>`;
}

/* ======================= tableau de bord ======================= */
export async function renderDashboard() {
  const view = $('#view');
  view.innerHTML = '<div class="empty">Chargement…</div>';
  let d;
  try { d = await api('/api/dashboard'); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const q = d.quota;

  view.innerHTML = `
    ${pageHead({ kicker: 'Candidatures spontanées', title: 'Mes campagnes',
                 sub: 'Une campagne = un poste, une cible, un mail qui part en ton nom vers une personne par entreprise, au rythme des quotas.',
                 right: `<a class="btn btn-accent" href="#/campagnes/nouvelle">+ Créer une campagne</a>` })}
    ${q.dry_run ? '<div class="banner info"><span><b>Mode simulation</b> — les campagnes se déroulent sans qu’aucun mail ne parte. Désactive-le dans les <a href="#/reglages" class="accent">réglages</a> quand tu es prêt.</span></div>' : ''}
    <div class="stats">
      ${statCard('Ce mois', `${q.month_sent}<small>/${q.month_cap}</small>`, `${Math.max(0, q.month_cap - q.month_sent)} restantes · ${q.day_sent}/${q.day_cap} aujourd’hui`)}
      ${statCard('Envoyés', d.totals.sent, 'sur l’ensemble des campagnes', 'tone-tech')}
      ${statCard('Ouverts', d.totals.opened, d.totals.sent ? `${pct(d.totals.opened, d.totals.sent)} % d’ouverture · ${d.totals.replied} réponse${d.totals.replied > 1 ? 's' : ''}` : 'aucun envoi encore', 'tone-ux')}
    </div>
    <div class="grid-2">
      <div>
        <div class="section-head"><h2><span class="emoji">✈️</span>Campagnes</h2></div>
        <div id="camps" style="display:grid;gap:14px"></div>
      </div>
      <div>
        <div class="section-head"><h2><span class="emoji">🔔</span>Journal d’activité</h2></div>
        <div class="card-white" style="padding:20px 22px">
          <div class="muted" style="font-size:13px;margin-bottom:14px">Ce qui s’est passé sur tes campagnes.</div>
          <div class="feed">${d.events.length ? d.events.map(eventLine).join('') : '<div class="muted">Rien pour l’instant.</div>'}</div>
        </div>
      </div>
    </div>`;

  const el = $('#camps');
  if (!d.campaigns.length) {
    el.innerHTML = `<div class="empty"><b>Aucune campagne</b>Crée la première : poste, zone, secteurs, mail — et l’outil trouve à qui écrire.</div>`;
    return;
  }
  el.innerHTML = d.campaigns.map(c => `
    <div class="card-white camp" data-id="${c.id}" style="cursor:pointer">
      <div><div class="name">${esc(c.name)}</div><div class="where">${esc([c.zone || 'France', (c.sectors || '').split(',').filter(Boolean).map(sectorLabel).slice(0, 3).join(', ')].filter(Boolean).join(' · '))}</div></div>
      <div style="display:flex;gap:8px;align-items:center"><span class="status-pill ${c.status}"><i></i>${CAMP_STATUS[c.status] || c.status}</span></div>
      <div class="nums">
        <div>Envoyés<b>${c.sent}</b></div><div>Ouverts<b>${c.opened}</b></div><div>Réponses<b>${c.replied}</b></div>
        <div>Programmés<b>${c.scheduled}</b></div>
      </div>
    </div>`).join('');
  $$('#camps .camp').forEach(x => x.onclick = () => location.hash = `#/campagnes/${x.dataset.id}`);
}

/* ======================= assistant ======================= */
const STEPS = ['Ta cible', 'Le marché', 'Les personnes', 'Ton CV', 'Ton mail', 'Récap'];

function wizardShell(step, inner, foot) {
  return `
    <div class="wizard-head">
      <button class="btn btn-white btn-sm" id="wzBack" ${step === 0 ? 'disabled' : ''}>←</button>
      <div class="steps">${STEPS.map((s, i) => `<i class="${i < step ? 'done' : i === step ? 'cur' : ''}" title="${s}"></i>`).join('')}</div>
      <span class="muted" style="font-size:12.5px;white-space:nowrap">${STEPS[step]} · ${step + 1}/${STEPS.length}</span>
    </div>
    <div class="wizard-body">${inner}</div>
    ${foot ? `<div class="wizard-foot">${foot}</div>` : ''}`;
}

export async function renderWizard() {
  if (!state.settings) { try { state.settings = await api('/api/settings'); } catch {} }
  const runId = queryParam('run');
  if (!state.wizard || (runId && state.wizard.runId !== +runId)) {
    const s = state.settings || {};
    state.wizard = { step: 0, job: '', cities: [], agglo: false, head: 'pme', min: '', max: '', limit: 25, market: null, sectors: new Set(),
                     showAll: false, runId: null, recipients: [], chosen: new Set(), excluded: {},
                     cv: s.settings?.cv_path || '', cvName: s.cv_name || '',
                     subject: s.defaults?.subject || '', body: s.defaults?.body || '', personalize: !!s.claude, preview: null, busy: false };
    if (runId) {
      try {
        const run = await api(`/api/runs/${runId}`);
        let p = {}; try { p = JSON.parse(run.params || '{}'); } catch {}
        const w = state.wizard;
        // Les colonnes de la recherche font foi si les paramètres sont absents
        // ou illisibles (anciennes recherches lancées depuis le CLI).
        w.job = run.job_title;
        w.cities = Array.isArray(p.cities) ? p.cities : [];
        w.agglo = !!p.agglomeration;
        w.sectors = new Set(p.sectors?.length ? p.sectors : (run.sectors || '').split(',').filter(Boolean));
        w.runId = +runId; w.step = 2;
        await loadRecipients();
      } catch (e) { toast(e.message); }
    }
  }
  drawWizard();
}

async function loadRecipients() {
  const w = state.wizard;
  const data = await api(`/api/runs/${w.runId}/recipients`);
  w.recipients = data.recipients; w.excluded = data.excluded;
  w.chosen = new Set(data.recipients.map(r => r.email));
}

function drawWizard() {
  const w = state.wizard, view = $('#view');
  const step = w.step;
  let inner = '', foot = '';

  if (step === 0) {
    inner = `
      <h1>Quel poste vises-tu<span class="dot-accent">?</span></h1>
      <div class="sub" style="color:var(--muted);margin:10px 0 28px">On cible les entreprises qui recrutent ce profil — même celles qui n’ont rien publié.</div>
      <div class="field" style="margin-bottom:20px"><label class="lbl">Titre du poste</label>
        <input class="input input-hero on-paper" id="wzJob" placeholder="ex. Product owner, développeur Python…" value="${esc(w.job)}"></div>
      <div class="field" style="margin-bottom:20px">
        <span class="lbl">Villes</span>
        <div id="wzCities"></div>
        <label class="check" style="margin-top:6px"><button type="button" class="switch ${w.agglo ? 'on' : ''}" id="wzAgglo"></button><span>Inclure toute l’agglomération <span class="muted">(Bordeaux → les 28 communes de la métropole)</span></span></label>
        <div class="hint">Plusieurs villes possibles. Aucune = toute la France.</div>
      </div>
      <div class="grid-3" style="grid-template-columns:1.6fr .8fr;margin-bottom:20px">
        <div class="field"><span class="lbl">Taille</span><div class="pills on-paper" id="wzHead">${HEADCOUNT.filter(h => h.key !== 'custom').map(h => `<button class="pill ${w.head === h.key ? 'on' : ''}" data-k="${h.key}">${h.label}</button>`).join('')}</div></div>
        <div class="field"><label class="lbl">Explorer</label><select class="input" id="wzLimit">${[25, 50, 100].map(n => `<option ${w.limit === n ? 'selected' : ''}>${n}</option>`).join('')}</select><div class="hint">entreprises</div></div>
      </div>`;
    foot = `<button class="btn btn-accent btn-lg btn-block" id="wzNext">Analyser le marché</button><span class="hint" id="wzHint"></span>`;
  }

  if (step === 1) {
    const rows = (w.market || []).filter(r => r.count > 0);
    const shown = w.showAll ? rows : rows.slice(0, 6);
    const total = rows.filter(r => w.sectors.has(r.key)).reduce((a, r) => a + r.count, 0);
    inner = `
      <p class="kicker">D’après ta cible · ${esc(w.job)} · ${esc(describeCities(w.cities, w.agglo))}</p>
      <h1>${total.toLocaleString('fr-FR')} entreprises ciblées<span class="dot-accent">.</span></h1>
      <div class="sub" style="color:var(--muted);margin:10px 0 24px">Tout est inclus par défaut. Retire ce que tu veux, le total se met à jour. On explorera les <b style="color:var(--ink)">${w.limit}</b> premières pour trouver à qui écrire.</div>
      <div class="card-white" id="wzSectors">
        ${shown.map(r => `<div class="sector-row"><span class="nm">${esc(r.label.split(' / ')[0])}</span><span class="ct">${r.count.toLocaleString('fr-FR')}</span>
          <span class="muted" style="font-size:12px;font-weight:600">${w.sectors.has(r.key) ? 'Inclus' : 'Exclu'}</span><button class="switch ${w.sectors.has(r.key) ? 'on' : ''}" data-k="${r.key}" aria-label="inclure"></button></div>`).join('')}
        ${rows.length > 6 ? `<div class="sector-row"><button class="btn btn-text btn-sm" id="wzMore">${w.showAll ? 'Réduire' : `Voir les ${rows.length - 6} autres secteurs`}</button></div>` : ''}
      </div>`;
    foot = `<button class="btn btn-accent btn-lg btn-block" id="wzNext" ${w.sectors.size ? '' : 'disabled'}>Trouver les interlocuteurs</button><span class="hint">Explore les sites des entreprises et en extrait les contacts. Une à deux minutes.</span>`;
  }

  if (step === 2) {
    const n = w.chosen.size, ex = w.excluded || {};
    const exTxt = [ex.deja_contactes ? `${ex.deja_contactes} déjà contactée${ex.deja_contactes > 1 ? 's' : ''}` : '',
                   ex.score_faible ? `${ex.score_faible} peu pertinent${ex.score_faible > 1 ? 's' : ''}` : '',
                   ex.domaine_tiers ? `${ex.domaine_tiers} prestataire${ex.domaine_tiers > 1 ? 's' : ''}` : '',
                   ex.hors_sujet ? `${ex.hors_sujet} hors sujet` : ''].filter(Boolean).join(' · ');
    inner = `
      <p class="kicker">Une personne par entreprise, la mieux placée</p>
      <h1>${n} personne${n > 1 ? 's' : ''} à qui écrire<span class="dot-accent">.</span></h1>
      <div class="sub" style="color:var(--muted);margin:10px 0 18px">Décoche celles que tu ne veux pas. ${exTxt ? `<span class="tag plain" style="margin-left:0">écartés : ${esc(exTxt)}</span>` : ''}</div>
      ${w.recipients.length ? '' : '<div class="banner"><span>Aucun contact exploitable dans cette recherche. Reviens en arrière pour élargir la cible.</span></div>'}
      ${w.recipients.length ? `<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
        <button class="btn btn-white btn-sm" id="wzBriefs" ${state.settings?.claude ? '' : 'disabled'}>🔎 Analyser les enjeux des entreprises</button>
        <span class="hint">${state.settings?.claude ? 'Claude lit le site de chaque entreprise et résume activité et enjeux — à relire avant d’écrire.' : 'Clé Claude absente : analyse indisponible.'}</span></div>` : ''}
      <div class="list recip card-white" style="padding:8px" id="wzRecip">
        ${w.recipients.map(r => { const name = [r.first_name, r.last_name].filter(Boolean).join(' ');
          return `<div class="item"><input type="checkbox" data-e="${esc(r.email)}" ${w.chosen.has(r.email) ? 'checked' : ''} style="accent-color:var(--logo-to);width:16px;height:16px">
            <div class="mono sm ${CAT_FAMILY[r.category] || ''}">${esc(monogram(r.company_name))}</div>
            <div style="min-width:0"><div class="t" style="font-size:14px">${esc(r.email)}${contactTags(r)}</div>
              <div class="s">${name ? `<b>${esc(name)}</b>` : ''}${name && r.role_title ? ' — ' : ''}${esc((r.role_title || '').slice(0, 60))}</div></div>
            <div class="meta" style="text-align:left"><b>${esc(pretty(r.company_name))}</b>${esc([r.company_city, r.company_size].filter(Boolean).join(' · '))}</div>
            <span class="catpill ${esc(r.category)}"><i></i>${esc(CATEGORY[r.category] || r.category)}</span>
            ${r.company_brief ? `<div class="detail" style="padding-left:78px;white-space:pre-line">${esc(r.company_brief)}</div>` : ''}</div>`; }).join('')}
      </div>`;
    foot = `<button class="btn btn-accent btn-lg btn-block" id="wzNext" ${n ? '' : 'disabled'}>Continuer avec ${n} destinataire${n > 1 ? 's' : ''}</button>`;
  }

  if (step === 3) {
    inner = `
      <h1>Quel CV veux-tu envoyer<span class="dot-accent">?</span></h1>
      <div class="sub" style="color:var(--muted);margin:10px 0 24px">Il part en pièce jointe de chaque mail. Tu pourras le changer dans les réglages.</div>
      <div class="card-white" style="padding:36px 24px;text-align:center;border:2px dashed var(--line)">
        ${w.cvName ? `<div class="mono biz" style="margin:0 auto 12px">✓</div><div style="font-weight:600">CV importé</div><div class="muted" style="font-size:13px">${esc(w.cvName)}</div>`
                   : `<div class="mono" style="margin:0 auto 12px">PDF</div><div style="font-weight:600">Aucun CV pour l’instant</div><div class="muted" style="font-size:13px">Dépose un PDF ci-dessous.</div>`}
      </div>
      <label class="btn btn-block" style="margin-top:12px;cursor:pointer">📎 ${w.cvName ? 'Remplacer par un autre PDF' : 'Choisir un PDF'}<input type="file" id="wzCv" accept="application/pdf" class="hidden"></label>`;
    foot = `<button class="btn btn-accent btn-lg btn-block" id="wzNext" ${w.cvName ? '' : 'disabled'}>Continuer avec ce CV</button>
            <button class="btn btn-text" id="wzSkipCv">Continuer sans CV</button>`;
  }

  if (step === 4) {
    const s = state.settings || {};
    const first = w.recipients.find(r => w.chosen.has(r.email));
    inner = `
      <p class="kicker">Rédigé pour ton profil · ${esc(w.job)}</p>
      <h1>Voici le mail qui partira en ton nom<span class="dot-accent">.</span></h1>
      <div class="sub" style="color:var(--muted);margin:10px 0 22px">Les <mark style="background:var(--ux);color:var(--ux-ink);padding:0 4px;border-radius:4px">{variables}</mark> se remplissent pour chaque entreprise. <b style="color:var(--ink)">{accroche}</b> est le paragraphe rédigé sur mesure.</div>
      <div class="field" style="margin-bottom:16px"><label class="lbl">Objet</label><input class="input on-paper" id="wzSubject" value="${esc(w.subject)}"></div>
      <div class="field" style="margin-bottom:12px"><label class="lbl">Corps</label><textarea class="input on-paper" id="wzBody">${esc(w.body)}</textarea></div>
      <div class="pills" style="margin-bottom:18px">${Object.keys(s.variables || {}).map(v => `<button class="pill" data-var="${v}" title="${esc(s.variables[v])}" style="background:var(--card)">{${v}}</button>`).join('')}</div>
      <label class="check" style="margin-bottom:22px"><button class="switch ${w.personalize ? 'on' : ''}" id="wzPers" type="button" ${s.claude ? '' : 'disabled'}></button>
        <span>Paragraphe <b style="color:var(--ink)">{accroche}</b> rédigé par Claude pour chaque entreprise${s.claude ? '' : ' — <b style="color:var(--ux-ink)">clé API absente</b>, repli sur des phrases par règles'}</span></label>
      <div class="section-head" style="margin-top:8px"><h3>Aperçu${first ? ` avec ${esc(pretty(first.company_name))}` : ''}</h3><button class="btn btn-white btn-sm" id="wzPreview" ${first ? '' : 'disabled'}>${w.preview ? 'Rafraîchir' : 'Générer l’aperçu'}</button></div>
      <div class="mail-preview" id="wzPrev">${w.preview
        ? `<span class="tag ex ${w.preview.engine === 'claude' ? 'ok' : 'plain'}">${w.preview.engine === 'claude' ? 'rédigé par Claude' : 'par règles'}</span><b>${esc(w.preview.subject)}</b>\n\n${esc(w.preview.body_text)}`
        : `<span class="muted">${highlightVars(w.subject)}\n\n${highlightVars(w.body)}</span>`}</div>`;
    foot = `<button class="btn btn-accent btn-lg btn-block" id="wzNext">Utiliser ce modèle</button><span class="hint">🔒 Réécrit pour chaque entreprise, avec ton nom et ton poste.</span>`;
  }

  if (step === 5) {
    const s = state.settings || {};
    const gmail = s.gmail || {};
    const sim = s.dry_run;
    const can = sim || gmail.connected;
    const n = w.chosen.size, cap = s.caps?.daily || 25;
    inner = `
      <h1>Ta campagne est prête<span class="dot-accent">.</span></h1>
      <div class="sub" style="color:var(--muted);margin:10px 0 24px">Vérifie une dernière fois, puis lance les envois.</div>
      <div class="recap">
        <div><span>Poste</span><b>${esc(w.job)}</b></div>
        <div><span>Cible</span><b>${esc(describeCities(w.cities, w.agglo))} · ${[...w.sectors].map(sectorLabel).slice(0, 3).join(', ')}${w.sectors.size > 3 ? ` +${w.sectors.size - 3}` : ''}</b></div>
        <div><span>Destinataires</span><b>${n} personne${n > 1 ? 's' : ''}, une par entreprise</b></div>
        <div><span>Rythme</span><b>${Math.min(n, cap)} par jour max, un toutes les ~4 min, 8 h – 19 h</b></div>
        <div><span>En ton nom, depuis</span><b>${sim ? 'Simulation — aucun envoi réel' : gmail.connected ? `Gmail · ${esc(gmail.email || '')}` : '<span style="color:#c53d3a">Gmail non connecté</span>'}</b></div>
        <div><span>Ton message</span><b>${w.personalize && s.claude ? 'Modèle + paragraphe Claude par entreprise' : 'Modèle avec variables'}</b></div>
        <div><span>CV joint</span><b>${esc(w.cvName || 'aucun')}</b></div>
      </div>`;
    foot = `<button class="btn btn-accent btn-lg btn-block" id="wzLaunch" ${can && n ? '' : 'disabled'}>${sim ? 'Lancer ma campagne (simulation)' : 'Lancer ma campagne'}</button>
            <button class="btn btn-text" id="wzDraft">Enregistrer en brouillon</button>
            <span class="hint">${can ? '🔒 Tu peux mettre en pause à tout moment.' : 'Connecte ton Gmail dans les <a href="#/reglages" class="accent">réglages</a>, ou active le mode simulation pour tester.'}</span>`;
  }

  view.innerHTML = wizardShell(step, inner, foot);
  bindWizard();
}

function bindWizard() {
  const w = state.wizard;
  const back = $('#wzBack'); if (back) back.onclick = () => { w.step = Math.max(0, w.step - 1); w.preview = null; drawWizard(); };

  if (w.step === 0) {
    $('#wzJob').oninput = e => w.job = e.target.value;
    cityPicker($('#wzCities'), w.cities, () => {});
    $('#wzAgglo').onclick = () => { w.agglo = !w.agglo; $('#wzAgglo').classList.toggle('on', w.agglo); };
    $('#wzLimit').onchange = e => w.limit = +e.target.value;
    $$('#wzHead .pill').forEach(b => b.onclick = () => { w.head = b.dataset.k; $$('#wzHead .pill').forEach(x => x.classList.toggle('on', x === b)); });
    $('#wzNext').onclick = async () => {
      if (!w.job.trim()) { $('#wzJob').classList.add('err'); $('#wzJob').focus(); return; }
      $('#wzNext').disabled = true; $('#wzHint').textContent = 'Comptage des entreprises par secteur…';
      try {
        w.market = await api('/api/market', { method: 'POST', body: JSON.stringify({ cities: w.cities, agglomeration: w.agglo, ...headcountRange(w.head, w.min, w.max) }) });
        w.sectors = new Set(w.market.filter(r => r.count > 0).map(r => r.key));
        w.step = 1; drawWizard();
      } catch (e) { $('#wzNext').disabled = false; $('#wzHint').className = 'hint err'; $('#wzHint').textContent = e.message; }
    };
  }

  if (w.step === 1) {
    $$('#wzSectors .switch').forEach(b => b.onclick = () => { const k = b.dataset.k; w.sectors.has(k) ? w.sectors.delete(k) : w.sectors.add(k); drawWizard(); });
    const more = $('#wzMore'); if (more) more.onclick = () => { w.showAll = !w.showAll; drawWizard(); };
    $('#wzNext').onclick = async () => {
      const payload = { job_title: w.job.trim(), sectors: [...w.sectors], cities: w.cities, agglomeration: w.agglo, limit: w.limit,
                        use_search_engine: true, verify_smtp: false, ...headcountRange(w.head, w.min, w.max) };
      try {
        await startSearch(payload, async (runId) => {
          w.runId = runId;
          try { await loadRecipients(); } catch (e) { toast(e.message); }
          w.step = 2; drawWizard();
        });
        const body = $('.wizard-body'); body.innerHTML = '<div class="card"><div class="card-body" id="wzProgress"></div></div>';
        $('.wizard-foot').innerHTML = '';
        renderProgress($('#wzProgress'));
      } catch (e) { toast(e.message); }
    };
  }

  if (w.step === 2) {
    $$('#wzRecip input[type=checkbox]').forEach(c => c.onchange = () => { c.checked ? w.chosen.add(c.dataset.e) : w.chosen.delete(c.dataset.e);
      const n = w.chosen.size; const b = $('#wzNext'); b.disabled = !n; b.textContent = `Continuer avec ${n} destinataire${n > 1 ? 's' : ''}`; $('h1').innerHTML = `${n} personne${n > 1 ? 's' : ''} à qui écrire<span class="dot-accent">.</span>`; });
    $('#wzNext').onclick = () => { w.step = 3; drawWizard(); };
    const briefs = $('#wzBriefs'); if (briefs) briefs.onclick = async () => {
      const todo = w.recipients.filter(r => !r.company_brief).length;
      briefs.disabled = true; briefs.textContent = `Analyse en cours… (${todo} entreprise${todo > 1 ? 's' : ''})`;
      try {
        const map = await api(`/api/runs/${w.runId}/briefs`, { method: 'POST', body: JSON.stringify({ job_title: w.job }) });
        w.recipients.forEach(r => { const b = map[r.company_siren || r.email]; if (b) r.company_brief = b; });
        toast(`${Object.keys(map).length} fiche${Object.keys(map).length > 1 ? 's' : ''} prête${Object.keys(map).length > 1 ? 's' : ''}`);
      } catch (e) { toast(e.message); }
      drawWizard();
    };
    enableTilt($('#wzRecip'), 2);
  }

  if (w.step === 3) {
    $('#wzCv').onchange = async (e) => {
      const file = e.target.files[0]; if (!file) return;
      const fd = new FormData(); fd.append('file', file);
      try { const r = await upload('/api/settings/cv', fd); w.cvName = r.cv_name; state.settings = null; toast('CV importé'); drawWizard(); }
      catch (err) { toast(err.message); }
    };
    $('#wzNext').onclick = () => { w.step = 4; drawWizard(); };
    $('#wzSkipCv').onclick = () => { w.cvName = ''; w.step = 4; drawWizard(); };
  }

  if (w.step === 4) {
    $('#wzSubject').oninput = e => w.subject = e.target.value;
    $('#wzBody').oninput = e => w.body = e.target.value;
    $$('[data-var]').forEach(b => b.onclick = () => { const ta = $('#wzBody'); const v = `{${b.dataset.var}}`;
      const s = ta.selectionStart; ta.value = ta.value.slice(0, s) + v + ta.value.slice(ta.selectionEnd); w.body = ta.value; ta.focus(); ta.selectionEnd = s + v.length; });
    const pers = $('#wzPers'); pers.onclick = () => { w.personalize = !w.personalize; pers.classList.toggle('on', w.personalize); };
    $('#wzPreview').onclick = async () => {
      const first = w.recipients.find(r => w.chosen.has(r.email)); if (!first) return;
      $('#wzPreview').disabled = true; $('#wzPreview').textContent = 'Rédaction…';
      try {
        w.preview = await api('/api/compose/preview', { method: 'POST', body: JSON.stringify({ job_title: w.job, subject_tpl: w.subject, body_tpl: w.body, personalize: w.personalize, application: first }) });
      } catch (e) { toast(e.message); }
      drawWizard();
    };
    $('#wzNext').onclick = () => { w.step = 5; drawWizard(); };
  }

  if (w.step === 5) {
    const create = async () => api('/api/campaigns', { method: 'POST', body: JSON.stringify({
      name: w.job.trim(), job_title: w.job.trim(), zone: w.cities.length ? describeCities(w.cities, w.agglo) : null, sectors: [...w.sectors], run_id: w.runId,
      recipients: [...w.chosen], subject_tpl: w.subject, body_tpl: w.body, personalize: w.personalize }) });
    $('#wzDraft').onclick = async () => { try { const c = await create(); state.wizard = null; toast('Brouillon enregistré'); location.hash = `#/campagnes/${c.id}`; } catch (e) { toast(e.message); } };
    $('#wzLaunch').onclick = async () => {
      $('#wzLaunch').disabled = true;
      try { const c = await create(); await api(`/api/campaigns/${c.id}/launch`, { method: 'POST' }); state.wizard = null; toast('Campagne lancée'); location.hash = `#/campagnes/${c.id}`; }
      catch (e) { $('#wzLaunch').disabled = false; toast(e.message); }
    };
  }
}

/* ======================= page de campagne ======================= */
let refreshTimer = null;

export async function renderCampaign(id) {
  clearInterval(refreshTimer);
  const view = $('#view');
  view.innerHTML = '<div class="empty">Chargement…</div>';
  let c;
  try { c = await api(`/api/campaigns/${id}`); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const cs = state.campaign;
  const q = c.quota, done = c.total ? pct(c.sent, c.total) : 0;
  const actions = c.status === 'brouillon' ? `<button class="btn btn-accent" data-act="launch">Lancer</button>`
    : c.status === 'active' ? `<button class="btn btn-white" data-act="pause">⏸ Mettre en pause</button>`
    : c.status === 'en_pause' ? `<button class="btn btn-accent" data-act="resume">▶ Reprendre</button>` : '';

  view.innerHTML = `
    <a href="#/campagnes" class="muted" style="font-size:13px;display:inline-flex;align-items:center;gap:6px;margin-bottom:16px">← Mes campagnes</a>
    <div class="card-white" style="padding:24px 28px;margin-bottom:22px">
      <div style="display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:flex-start">
        <div><div class="name" style="font-family:var(--font-serif);font-weight:700;font-size:28px">${esc(c.name)}</div>
          <div class="muted" style="font-size:13px;margin-top:4px">${esc(c.zone || 'France')} · créée le ${fmtDay(c.created_at)} · <span class="status-pill ${c.status}"><i></i>${CAMP_STATUS[c.status]}</span></div></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">${actions}<button class="btn btn-white" data-act="edit">✎ Modifier le mail</button><button class="btn btn-text btn-danger" data-act="delete" title="Supprimer">🗑</button></div>
      </div>
      <div style="display:flex;align-items:center;gap:14px;margin-top:18px;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)">
        <span>Complétion <b style="color:var(--ink)">${done} %</b></span><div class="bar-track" style="flex:1"><i style="width:${done}%"></i></div><span>${c.sent} / ${c.total} envois</span>
      </div>
    </div>
    ${q.dry_run ? '<div class="banner info"><span><b>Simulation</b> — les envois sont journalisés mais aucun mail ne part.</span></div>' : ''}
    ${c.status === 'active' && !q.can_send && q.reason ? `<div class="banner plain" style="background:var(--card);color:var(--muted)"><span>En attente : ${esc(q.reason)}.</span></div>` : ''}
    <p class="kicker">Analyse campagne</p>
    <div class="stats">
      ${statCard('Ce mois', `${q.month_sent}<small>/${q.month_cap}</small>`, `${Math.max(0, q.month_cap - q.month_sent)} restantes · toutes campagnes`)}
      ${statCard('Envoyés', c.sent, `${c.scheduled} programmé${c.scheduled > 1 ? 's' : ''}${c.failed ? ` · ${c.failed} échec${c.failed > 1 ? 's' : ''}` : ''}`, 'tone-tech')}
      ${statCard('Ouverts', c.opened, c.sent ? `${pct(c.opened, c.sent)} % d’ouverture · ${c.replied} réponse${c.replied > 1 ? 's' : ''}` : 'en attente d’envois', 'tone-ux')}
    </div>
    <div style="text-align:center;margin-bottom:22px"><button class="btn btn-white btn-sm" id="cpAct">${cs.activity ? 'Masquer l’activité détaillée ⌃' : 'Voir l’activité & les priorités ⌄'}</button></div>
    <div class="grid-2 ${cs.activity ? '' : 'hidden'}" id="cpActivity" style="margin-bottom:22px">
      <div class="card-white" style="padding:20px 22px"><h3>Journal d’activité</h3><div class="muted" style="font-size:13px;margin:4px 0 14px">Ce qui s’est passé sur ta campagne.</div><div class="feed" id="cpFeed"></div></div>
      <div class="card-white" style="padding:20px 22px"><h3>À suivre en priorité</h3><div class="muted" style="font-size:13px;margin:4px 0 14px">Les personnes qui ouvrent sans répondre : à relancer.</div><div class="list" id="cpFollow"></div></div>
    </div>
    <div class="card-white" style="padding:22px 24px">
      <div style="display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
        <div><h3>Mes candidatures spontanées</h3><div class="muted" style="font-size:13px" id="cpCount"></div></div>
        <div class="pills on-paper" id="cpSeg"></div>
      </div>
      <input class="input on-paper" id="cpQ" placeholder="🔍 Rechercher une entreprise…" style="margin-bottom:12px" value="${esc(cs.q)}">
      <div style="overflow-x:auto"><table class="tbl"><thead><tr><th>Entreprise</th><th>Contact cible</th><th>Poste</th><th>Envoyée</th><th>Statut</th><th>Candidature</th><th>LinkedIn</th><th></th></tr></thead><tbody id="cpRows"></tbody></table></div>
      <div class="pager" id="cpPager"></div>
    </div>`;

  $$('[data-act]').forEach(b => b.onclick = () => campaignAction(c, b.dataset.act));
  $('#cpAct').onclick = () => { cs.activity = !cs.activity; $('#cpActivity').classList.toggle('hidden', !cs.activity); $('#cpAct').textContent = cs.activity ? 'Masquer l’activité détaillée ⌃' : 'Voir l’activité & les priorités ⌄'; if (cs.activity) loadActivity(id); };
  $('#cpQ').oninput = (e) => { cs.q = e.target.value; cs.page = 1; loadTable(id); };
  loadTable(id);
  if (cs.activity) loadActivity(id);
  if (c.status === 'active') refreshTimer = setInterval(() => { if (location.hash === `#/campagnes/${id}`) { loadTable(id); if (cs.activity) loadActivity(id); } else clearInterval(refreshTimer); }, 30000);
}

async function loadActivity(id) {
  const [events, follow] = await Promise.all([api(`/api/campaigns/${id}/events`), api(`/api/campaigns/${id}/followups`)]);
  const f = $('#cpFeed'); if (f) f.innerHTML = events.length ? events.map(eventLine).join('') : '<div class="muted">Rien pour l’instant.</div>';
  const fl = $('#cpFollow'); if (fl) fl.innerHTML = follow.length ? follow.map(r => `<div class="item" style="grid-template-columns:40px 1fr auto">
      <div class="mono sm ${CAT_FAMILY[r.category] || ''}">${esc(monogram(r.company_name))}</div>
      <div><div class="t" style="font-size:14px">${esc(pretty(r.company_name))}</div><div class="s">ouvert ${r.opens}× · ${esc([r.first_name, r.last_name].filter(Boolean).join(' ') || r.email)}</div></div>
      <a class="btn btn-white btn-sm" href="${linkedinSearch(r)}" target="_blank" rel="noopener" title="Chercher sur LinkedIn">in</a></div>`).join('')
    : '<div class="muted" style="font-size:13px">Personne n’a encore ouvert.</div>';
}

async function loadTable(id) {
  const cs = state.campaign;
  const qs = new URLSearchParams({ page: cs.page, size: 10, q: cs.q, ...(cs.filter ? { status: cs.filter } : {}) });
  const d = await api(`/api/campaigns/${id}/applications?${qs}`);
  const total = Object.values(d.counts).reduce((a, b) => a + b, 0);
  $('#cpCount').textContent = `${total} candidature${total > 1 ? 's' : ''}`;
  $('#cpSeg').innerHTML = [['', 'Toutes', total], ...Object.entries(APP_STATUS).map(([k, l]) => [k, l, d.counts[k] || 0])]
    .filter(([k, , n]) => !k || n || k === cs.filter)
    .map(([k, l, n]) => `<button class="pill ${cs.filter === k ? 'on' : ''}" data-k="${k}">${l} <span class="n">${n}</span></button>`).join('');
  $$('#cpSeg .pill').forEach(b => b.onclick = () => { cs.filter = b.dataset.k; cs.page = 1; loadTable(id); });

  const tb = $('#cpRows');
  tb.innerHTML = d.rows.length ? d.rows.map(r => `<tr>
      <td class="co">${esc(pretty(r.company_name))}<small>${esc(r.company_domain || r.company_city || '')}</small></td>
      <td>${esc([r.first_name, r.last_name].filter(Boolean).join(' ') || r.email)}</td>
      <td class="muted">${esc((r.role_title || CATEGORY[r.category] || '').slice(0, 40))}</td>
      <td>${r.sent_at ? fmtDay(r.sent_at) : r.scheduled_at ? `<span class="muted">${fmtDate(r.scheduled_at)}</span>` : '—'}</td>
      <td><span class="st ${r.status}">${APP_STATUS[r.status] || r.status}</span>${r.opens ? `<span class="muted" style="font-size:11px;margin-left:6px">×${r.opens}</span>` : ''}</td>
      <td><button class="btn btn-white btn-sm" data-view="${r.id}">✉ Voir</button></td>
      <td><a class="btn btn-white btn-sm" href="${linkedinSearch(r)}" target="_blank" rel="noopener">in</a></td>
      <td>${r.status === 'programme' ? `<button class="btn btn-text btn-sm" data-cancel="${r.id}" title="Annuler cet envoi">×</button>` : ''}</td>
    </tr>`).join('') : `<tr><td colspan="8" class="muted" style="text-align:center;padding:28px">Aucune candidature${cs.filter || cs.q ? ' pour ce filtre' : ''}.</td></tr>`;

  const pages = Math.max(1, Math.ceil(d.total / 10));
  $('#cpPager').innerHTML = `<span>Affichage de ${d.total ? (cs.page - 1) * 10 + 1 : 0} à ${Math.min(cs.page * 10, d.total)} sur ${d.total}</span>
    <div class="pages">${Array.from({ length: pages }, (_, i) => i + 1).filter(p => p === 1 || p === pages || Math.abs(p - cs.page) <= 2).map(p => `<button class="${p === cs.page ? 'on' : ''}" data-p="${p}">${p}</button>`).join('')}</div>`;
  $$('#cpPager [data-p]').forEach(b => b.onclick = () => { cs.page = +b.dataset.p; loadTable(id); });
  $$('[data-view]').forEach(b => b.onclick = () => showApplication(+b.dataset.view));
  $$('[data-cancel]').forEach(b => b.onclick = async () => { if (!confirm('Annuler cet envoi ?')) return; await api(`/api/applications/${b.dataset.cancel}/cancel`, { method: 'POST' }); toast('Annulé'); loadTable(id); });
}

async function showApplication(appId) {
  let a = await api(`/api/applications/${appId}`);
  const render = () => openModal(`
    <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start">
      <div><p class="kicker">${esc(pretty(a.company_name))} · <span class="st ${a.status}">${APP_STATUS[a.status]}</span></p>
        <h3>${esc(a.subject || 'Mail pas encore rédigé')}</h3>
        <div class="muted" style="font-size:13px;margin-top:4px">À ${esc(a.email)}${a.sent_at ? ` · envoyé le ${fmtDay(a.sent_at)}` : a.scheduled_at ? ` · programmé ${fmtDate(a.scheduled_at)}` : ''}${a.opens ? ` · ouvert ${a.opens}×` : ''}</div></div>
      <button class="btn btn-text" id="mdClose">×</button>
    </div>
    ${a.company_brief ? `<div class="banner info" style="margin-top:16px;white-space:pre-line;display:block"><b>Enjeux compris</b>\n${esc(a.company_brief)}</div>` : ''}
    <div class="mail-preview" style="margin-top:${a.company_brief ? 12 : 18}px;box-shadow:none;background:var(--card)">${a.body_text ? esc(a.body_text) : '<span class="muted">Le mail est rédigé au moment de l’envoi. Tu peux le rédiger maintenant pour le relire.</span>'}</div>
    ${a.error ? `<div class="banner danger" style="margin-top:12px"><span>${esc(a.error)}</span></div>` : ''}
    <div style="display:flex;gap:8px;margin-top:18px;justify-content:flex-end">${a.status === 'programme' ? `<button class="btn btn-white" id="mdCompose">${a.body_text ? 'Rédiger à nouveau' : 'Rédiger maintenant'}</button>` : ''}</div>`);
  render();
  const bind = () => {
    $('#mdClose').onclick = closeModal;
    const c = $('#mdCompose'); if (c) c.onclick = async () => { c.disabled = true; c.textContent = 'Rédaction…'; try { a = await api(`/api/applications/${appId}/preview`, { method: 'POST' }); render(); bind(); } catch (e) { toast(e.message); } };
  };
  bind();
}

async function campaignAction(c, act) {
  try {
    if (act === 'launch') { await api(`/api/campaigns/${c.id}/launch`, { method: 'POST' }); toast('Campagne lancée'); }
    if (act === 'pause') { await api(`/api/campaigns/${c.id}/pause`, { method: 'POST' }); toast('En pause'); }
    if (act === 'resume') { await api(`/api/campaigns/${c.id}/resume`, { method: 'POST' }); toast('Reprise'); }
    if (act === 'delete') { if (!confirm(`Supprimer la campagne « ${c.name} » et ses candidatures ?`)) return; await api(`/api/campaigns/${c.id}`, { method: 'DELETE' }); toast('Supprimée'); location.hash = '#/campagnes'; return; }
    if (act === 'edit') { editTemplate(c); return; }
    renderCampaign(c.id);
  } catch (e) { toast(e.message); }
}

function editTemplate(c) {
  openModal(`
    <div style="display:flex;justify-content:space-between;align-items:flex-start"><h3>Modifier le mail</h3><button class="btn btn-text" id="mdClose">×</button></div>
    <div class="hint" style="margin:6px 0 16px">S’applique aux envois qui ne sont pas encore rédigés. Les {variables} sont remplies pour chaque entreprise.</div>
    <div class="field" style="margin-bottom:12px"><label class="lbl">Objet</label><input class="input on-paper" id="edSubject" value="${esc(c.subject_tpl || '')}"></div>
    <div class="field" style="margin-bottom:12px"><label class="lbl">Corps</label><textarea class="input on-paper" id="edBody">${esc(c.body_tpl || '')}</textarea></div>
    <label class="check" style="margin-bottom:16px"><button class="switch ${c.personalize ? 'on' : ''}" id="edPers" type="button"></button><span>Paragraphe {accroche} rédigé par Claude</span></label>
    <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn btn-primary" id="edSave">Enregistrer</button></div>`);
  let pers = !!c.personalize;
  $('#mdClose').onclick = closeModal;
  $('#edPers').onclick = () => { pers = !pers; $('#edPers').classList.toggle('on', pers); };
  $('#edSave').onclick = async () => {
    await api(`/api/campaigns/${c.id}`, { method: 'PATCH', body: JSON.stringify({ subject_tpl: $('#edSubject').value, body_tpl: $('#edBody').value, personalize: pers }) });
    closeModal(); toast('Mail mis à jour'); renderCampaign(c.id);
  };
}
