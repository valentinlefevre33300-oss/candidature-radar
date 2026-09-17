/* Vue : Tableau de bord — envoyés, ouverts, réponses par nature, relances proposées. */
import { $, $$, esc, api, toast, fmtDate, fmtDay, relTime, monogram, pretty, pageHead, enableTilt,
         openModal, closeModal, linkedinSearch, CAT_FAMILY, CAMP_STATUS } from './core.js';

const KIND = { refus: 'Refus', interet: 'Intérêt', question: 'Question', absence: 'Absence', autre: 'Autre' };
const pct = (a, b) => b ? Math.round(100 * a / b) : 0;
const tile = (k, v, d, tone = '') => `<div class="stat ${tone}"><div class="k">${k}</div><div class="v">${v}</div>${d ? `<div class="d">${d}</div>` : ''}</div>`;
const daysAgo = (iso) => Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 86400000));

export async function renderDashboard() {
  const view = $('#view');
  view.innerHTML = '<div class="empty">Chargement…</div>';
  let d;
  try { d = await api('/api/outcomes'); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  const t = d.totals, m = d.month;
  const maxDay = Math.max(1, ...d.days.map(x => x.sent));

  view.innerHTML = `
    ${pageHead({ kicker: 'Résultats · toutes campagnes', title: 'Tableau de bord',
                 sub: 'Ce que tes envois ont donné, les réponses reçues, et qui relancer.',
                 right: `<button class="btn btn-white" id="dbSync">↻ Lire les réponses</button>` })}
    ${d.gmail.missing_scopes && d.gmail.missing_scopes.length ? `<div class="banner"><span><b>Nouvelle permission Gmail</b> — pour lire les réponses (et distinguer un refus d’un intérêt), l’outil doit relire les fils de ses propres mails. <a href="/api/gmail/connect" class="accent">Reconnecter mon Gmail</a> — dix secondes.</span></div>` : ''}
    ${d.quota.dry_run ? '<div class="banner info"><span><b>Simulation</b> — les chiffres ci-dessous incluent des envois simulés.</span></div>' : ''}

    <div class="stats six">
      ${tile('Envoyés', t.sent, `${m.sent} ce mois`)}
      ${tile('Ouverts', t.opened, t.sent ? `${pct(t.opened, t.sent)} % des envoyés` : '—', 'tone-ux')}
      ${tile('Réponses', t.replied, t.sent ? `${pct(t.replied, t.sent)} % des envoyés` : '—', 'tone-tech')}
      ${tile('Intérêt', t.interet, t.replied ? `${pct(t.interet, t.replied)} % des réponses` : '—', 'tone-biz')}
      ${tile('Refus', t.refus, t.replied ? `${pct(t.refus, t.replied)} % des réponses` : '—', 'tone-data')}
      ${tile('Relances', t.followups, `${d.followups.length} proposée${d.followups.length > 1 ? 's' : ''}`)}
    </div>

    <div class="grid-2" style="margin-bottom:22px">
      <div class="card-white" style="padding:20px 22px">
        <h3>Entonnoir</h3>
        <div class="muted" style="font-size:13px;margin:4px 0 14px">De l’envoi à l’intérêt, sur l’ensemble des campagnes.</div>
        <div class="funnel">
          ${[['Envoyés', t.sent], ['Ouverts', t.opened], ['Réponses', t.replied], ['Intérêt', t.interet]].map(([l, v]) =>
            `<div class="row"><span class="lbl">${l}</span><div class="bar"><i style="width:${pct(v, t.sent)}%"></i></div><span class="val"><b>${v}</b> <span class="muted">${t.sent ? pct(v, t.sent) + ' %' : ''}</span></span></div>`).join('')}
        </div>
      </div>
      <div class="card-white" style="padding:20px 22px">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px"><div><h3>14 derniers jours</h3><div class="muted" style="font-size:13px;margin-top:4px">Envois par jour · survole pour le détail.</div></div>
          <button class="btn btn-text btn-sm" id="dbTable">Tableau</button></div>
        <div class="days" id="dbDays" style="margin-top:14px">
          ${d.days.map(x => `<div class="d"><span class="tooltip">${fmtDay(x.day)} · ${x.sent} envoyé${x.sent > 1 ? 's' : ''} · ${x.opened} ouvert${x.opened > 1 ? 's' : ''} · ${x.replied} réponse${x.replied > 1 ? 's' : ''}</span><i style="height:${Math.max(2, Math.round(100 * x.sent / maxDay))}%"></i></div>`).join('')}
        </div>
        <div class="muted" style="display:flex;justify-content:space-between;font-size:11px;margin-top:6px"><span>${fmtDay(d.days[0].day)}</span><span>${fmtDay(d.days[d.days.length - 1].day)}</span></div>
        <table class="tbl hidden" id="dbDaysTable" style="margin-top:12px;font-size:12.5px"><thead><tr><th>Jour</th><th>Envoyés</th><th>Ouverts</th><th>Réponses</th></tr></thead>
          <tbody>${d.days.filter(x => x.sent || x.opened || x.replied).map(x => `<tr><td>${fmtDay(x.day)}</td><td>${x.sent}</td><td>${x.opened}</td><td>${x.replied}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">Aucune activité.</td></tr>'}</tbody></table>
      </div>
    </div>

    <section class="section" style="margin-top:8px">
      <div class="section-head"><h2><span class="emoji">🔁</span>À relancer</h2><span class="muted" style="font-size:13px">sans réponse après ${d.rules.after_days} jours, ${d.rules.after_open_days} si le mail a été ouvert · une seule relance par personne</span></div>
      <div class="list" id="dbFollow"></div>
    </section>

    <section class="section">
      <div class="section-head"><h2><span class="emoji">💬</span>Réponses</h2></div>
      <div class="list" id="dbReplies"></div>
    </section>

    <section class="section">
      <div class="section-head"><h2><span class="emoji">✈️</span>Par campagne</h2></div>
      <div class="card-white" style="padding:8px 12px;overflow-x:auto">
        <table class="tbl"><thead><tr><th>Campagne</th><th>Statut</th><th>Envoyés</th><th>Ouverts</th><th>Réponses</th><th>Intérêt</th><th>Refus</th><th>Relances</th></tr></thead>
        <tbody>${d.by_campaign.length ? d.by_campaign.map(c => `<tr><td class="co"><a href="#/campagnes/${c.id}">${esc(c.name)}</a><small>${esc(c.job_title)}</small></td>
          <td><span class="status-pill ${c.status}"><i></i>${CAMP_STATUS[c.status] || c.status}</span></td><td>${c.sent}</td><td>${c.opened}${c.sent ? ` <span class="muted">${pct(c.opened, c.sent)} %</span>` : ''}</td><td>${c.replied}</td><td>${c.interet}</td><td>${c.refus}</td><td>${c.followups}</td></tr>`).join('')
          : '<tr><td colspan="8" class="muted" style="text-align:center;padding:24px">Aucune campagne.</td></tr>'}</tbody></table>
      </div>
    </section>`;

  $('#dbSync').onclick = async () => {
    $('#dbSync').disabled = true; $('#dbSync').textContent = 'Lecture…';
    try { const r = await api('/api/replies/sync', { method: 'POST' }); toast(r.replies ? `${r.replies} nouvelle${r.replies > 1 ? 's' : ''} réponse${r.replies > 1 ? 's' : ''}` : 'Aucune nouvelle réponse'); renderDashboard(); }
    catch (e) { toast(e.message); $('#dbSync').disabled = false; $('#dbSync').textContent = '↻ Lire les réponses'; }
  };
  $('#dbTable').onclick = () => { const tb = $('#dbDaysTable'); tb.classList.toggle('hidden'); $('#dbDays').classList.toggle('hidden', !tb.classList.contains('hidden')); $('#dbTable').textContent = tb.classList.contains('hidden') ? 'Tableau' : 'Graphique'; };

  drawFollowups(d.followups);
  drawReplies(d.replies);
}

function drawFollowups(rows) {
  const el = $('#dbFollow');
  if (!rows.length) { el.innerHTML = '<div class="empty"><b>Personne à relancer</b>Les candidatures sans réponse apparaîtront ici passé le délai.</div>'; return; }
  el.innerHTML = rows.map(r => {
    const name = [r.first_name, r.last_name].filter(Boolean).join(' ');
    return `<div class="item" style="grid-template-columns:52px minmax(0,1.4fr) minmax(0,1fr) auto">
      <div class="mono ${CAT_FAMILY[r.category] || ''}">${esc(monogram(r.company_name))}</div>
      <div style="min-width:0"><div class="t">${esc(pretty(r.company_name))}</div><div class="s">${name ? `<b>${esc(name)}</b>` : esc(r.email)}${r.role_title ? ' — ' + esc(r.role_title.slice(0, 50)) : ''}</div></div>
      <div class="meta" style="text-align:left"><b>envoyé il y a ${daysAgo(r.sent_at)} j</b>${r.opens ? `ouvert ${r.opens}× · ${relTime(r.last_open_at)}` : 'jamais ouvert'} · ${esc(r.campaign_name)}</div>
      <div class="actions"><a class="btn btn-white btn-sm" href="${linkedinSearch(r)}" target="_blank" rel="noopener">in</a><button class="btn btn-primary btn-sm" data-follow="${r.id}">Préparer la relance</button></div>
    </div>`;
  }).join('');
  $$('[data-follow]', el).forEach(b => b.onclick = () => prepareFollowup(+b.dataset.follow, b));
  enableTilt(el, 3);
}

async function prepareFollowup(id, btn) {
  btn.disabled = true; btn.textContent = 'Rédaction…';
  let p;
  try { p = await api(`/api/applications/${id}/followup/preview`, { method: 'POST' }); }
  catch (e) { toast(e.message); btn.disabled = false; btn.textContent = 'Préparer la relance'; return; }
  btn.disabled = false; btn.textContent = 'Préparer la relance';
  openModal(`
    <div style="display:flex;justify-content:space-between;align-items:flex-start"><div><p class="kicker">Relance · ${esc(pretty(p.company_name))}</p><h3>À ${esc(p.to)}</h3></div><button class="btn btn-text" id="mdClose">×</button></div>
    <div class="hint" style="margin:8px 0 14px">Envoyée dans le fil du premier mail, sans pièce jointe. Retouche le texte si tu veux, puis envoie.</div>
    <div class="field" style="margin-bottom:12px"><label class="lbl">Objet</label><input class="input on-paper" id="fuSubject" value="${esc(p.subject)}"></div>
    <div class="field" style="margin-bottom:16px"><label class="lbl">Message</label><textarea class="input on-paper" id="fuBody" style="min-height:220px">${esc(p.body_text)}</textarea></div>
    <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn btn-text" id="fuCancel">Plus tard</button><button class="btn btn-accent" id="fuSend">Envoyer la relance</button></div>`);
  $('#mdClose').onclick = closeModal; $('#fuCancel').onclick = closeModal;
  $('#fuSend').onclick = async () => {
    $('#fuSend').disabled = true; $('#fuSend').textContent = 'Envoi…';
    try { const r = await api(`/api/applications/${id}/followup`, { method: 'POST', body: JSON.stringify({ subject: $('#fuSubject').value, body_text: $('#fuBody').value }) });
      closeModal(); toast(r.simulation ? 'Relance simulée' : 'Relance envoyée'); renderDashboard(); }
    catch (e) { toast(e.message); $('#fuSend').disabled = false; $('#fuSend').textContent = 'Envoyer la relance'; }
  };
}

function drawReplies(rows) {
  const el = $('#dbReplies');
  if (!rows.length) { el.innerHTML = '<div class="empty"><b>Aucune réponse pour l’instant</b>Les réponses sont lues toutes les dix minutes dans les fils de tes envois — ou tout de suite avec « Lire les réponses ».</div>'; return; }
  el.innerHTML = rows.map(r => {
    const name = [r.first_name, r.last_name].filter(Boolean).join(' ');
    return `<div class="item" style="grid-template-columns:52px minmax(0,1.2fr) minmax(0,1.6fr) auto auto">
      <div class="mono ${CAT_FAMILY[r.category] || ''}">${esc(monogram(r.company_name))}</div>
      <div style="min-width:0"><div class="t">${esc(pretty(r.company_name))}</div><div class="s">${name ? `<b>${esc(name)}</b>` : esc(r.email)} · ${fmtDate(r.replied_at)}</div></div>
      <div class="s" style="white-space:pre-line">${esc((r.reply_excerpt || '').slice(0, 220))}</div>
      <select class="kind ${esc(r.reply_kind || 'autre')}" data-id="${r.id}">${Object.entries(KIND).map(([k, l]) => `<option value="${k}" ${k === r.reply_kind ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <div class="actions"><a class="btn btn-white btn-sm" href="${linkedinSearch(r)}" target="_blank" rel="noopener">in</a></div>
    </div>`;
  }).join('');
  $$('select.kind', el).forEach(s => s.onchange = async () => {
    await api(`/api/applications/${s.dataset.id}/reply`, { method: 'PATCH', body: JSON.stringify({ reply_kind: s.value }) });
    s.className = `kind ${s.value}`; toast(KIND[s.value]);
  });
}
