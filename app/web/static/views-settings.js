/* Vue : Réglages — Gmail, expéditeur, CV, envois, rédaction. */
import { $, $$, esc, api, upload, state, toast, pageHead, queryParam } from './core.js';

/* Exemple rendu côté navigateur : les variables remplies avec une entreprise fictive. */
const SAMPLE = { salutation: 'Bonjour Claire Martin,', prenom: 'Claire', nom: 'Martin', entreprise: 'Atelier Nova', poste: '', ville: 'Bordeaux',
                 secteur: 'Tech', taille: '20 à 49 salariés', moi: '', signature: '', linkedin: '', portfolio: '', liens: '',
                 ouverture: 'Vous pilotez le produit chez Atelier Nova : c’est dans votre équipe que je souhaite rejoindre, en tant que product owner.',
                 accroche: 'Votre plateforme de réservation grandit vite ; j’ai déjà mené ce type de chantier, de la priorisation au suivi des équipes.' };
function renderSample(tpl, v, job) {
  const me = v.sender_name || 'Ton nom';
  const links = [v.linkedin_url ? `LinkedIn : ${v.linkedin_url}` : '', v.portfolio_url ? `Portfolio : ${v.portfolio_url}` : ''].filter(Boolean).join('\n');
  const ctx = { ...SAMPLE, poste: job || 'product owner', moi: me, signature: v.signature || me, linkedin: v.linkedin_url || '', portfolio: v.portfolio_url || '', liens: links };
  return (tpl || '').replace(/\{(\w+)\}/g, (m, k) => (k in ctx ? ctx[k] : m)).replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}
function bindTemplateEditor(s) {
  const v = s.settings, subj = $('#tplSubject'), body = $('#tplBody'), prev = $('#tplPreview');
  const draw = () => { prev.innerHTML = `<b>${esc(renderSample(subj.value, v))}</b>\n\n${esc(renderSample(body.value, v))}`; };
  subj.oninput = draw; body.oninput = draw; draw();
  $$('#tplVars [data-var]').forEach(b => b.onclick = () => {
    const tag = `{${b.dataset.var}}`, ta = document.activeElement === subj ? subj : body;
    const at = ta.selectionStart ?? ta.value.length;
    ta.value = ta.value.slice(0, at) + tag + ta.value.slice(ta.selectionEnd ?? at);
    ta.focus(); ta.selectionEnd = at + tag.length; draw();
  });
  $('#tplSave').onclick = async () => {
    const factory = s.factory || {};
    const same = subj.value.trim() === factory.subject && body.value.trim() === factory.body;
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ subject_tpl: same ? '' : subj.value, body_tpl: same ? '' : body.value }) });
    toast('Modèle enregistré'); state.settings = null;
  };
  $('#tplReset').onclick = async () => {
    subj.value = s.factory?.subject || ''; body.value = s.factory?.body || ''; draw();
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ subject_tpl: '', body_tpl: '' }) });
    toast('Modèle d’origine rétabli'); state.settings = null;
  };
}

export async function renderSettings() {
  const view = $('#view');
  view.innerHTML = '<div class="empty">Chargement…</div>';
  let s;
  try { s = await api('/api/settings'); } catch (e) { view.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
  state.settings = s;
  const g = s.gmail, v = s.settings;
  const flash = queryParam('gmail');
  const flashHtml = flash === 'ok' ? '<div class="banner ok"><span><b>Gmail connecté.</b> Les campagnes partiront en ton nom.</span></div>'
    : flash === 'refus' ? '<div class="banner"><span>Tu as refusé l’accès. Rien n’a été enregistré.</span></div>'
    : flash === 'erreur' ? '<div class="banner danger"><span>La connexion a échoué. Vérifie le client OAuth et recommence.</span></div>' : '';

  view.innerHTML = `
    ${pageHead({ kicker: 'Ton compte, tes envois', title: 'Réglages', sub: 'Tout ce qui part en ton nom se règle ici.' })}
    ${flashHtml}
    <div class="grid-2" style="grid-template-columns:1fr 1fr;align-items:start">
      <section class="card-white" style="padding:24px 26px">
        <h3>Gmail</h3>
        <div class="muted" style="font-size:13px;margin:4px 0 16px">Les mails partent depuis ta boîte, avec ton CV en pièce jointe. On lit aussi les en-têtes de tes fils pour détecter les réponses — jamais le contenu.</div>
        ${g.connected ? `
          ${g.missing_scopes && g.missing_scopes.length ? `<div class="banner" style="margin-bottom:14px"><span><b>Nouvelle permission</b> — cette version lit les réponses reçues dans les fils de ses propres mails (pour distinguer un refus d’un intérêt). <a href="/api/gmail/connect" class="accent">Reconnecter mon Gmail</a> pour l’accorder.</span></div>` : ''}
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap"><span class="mono biz sm">✓</span><div><b>${esc(g.email || 'Compte connecté')}</b><div class="muted" style="font-size:12.5px">envoi + lecture des réponses dans les fils envoyés</div></div>
          <span style="flex:1"></span><button class="btn btn-white btn-sm" id="gmDisc">Déconnecter</button></div>
          <div style="display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap">
            <input class="input on-paper" id="gmTestJob" placeholder="Poste pour le test" value="développeur Python" style="max-width:240px;padding:8px 12px">
            <button class="btn btn-primary btn-sm" id="gmTest">✉ M'envoyer un mail de test</button>
          </div>
          <div class="hint" style="margin-top:8px">Un vrai envoi, vers ta boîte uniquement, avec ton CV et un paragraphe rédigé sur une vraie entreprise. Pour te relire avant de lancer.</div>`
        : g.configured ? `
          ${g.needs_reconnect ? `<div class="banner" style="margin-bottom:14px"><span><b>Autorisation expirée</b> — Google la limite à 7 jours pour une application en mode test${g.email ? ` (${esc(g.email)})` : ''}. Les campagnes actives sont en pause ; reconnecte-toi puis reprends-les.</span></div>` : ''}
          <a class="btn btn-primary btn-block" href="/api/gmail/connect">${g.needs_reconnect ? 'Reconnecter mon Gmail' : 'Continuer avec Google'}</a>
          <div class="hint" style="margin-top:10px;text-align:center">Gratuit · 10 secondes · révocable depuis ton compte Google · à refaire tous les 7 jours (mode test)</div>`
        : `
          <div class="banner" style="margin-bottom:14px"><span><b>Client OAuth manquant.</b> Il faut d’abord créer un identifiant Google — une fois pour toutes.</span></div>
          <ol style="margin:0;padding-left:18px;font-size:13.5px;line-height:1.7;color:var(--muted)">
            <li>Ouvre <a class="accent" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">console.cloud.google.com → Identifiants</a>, crée un projet si besoin.</li>
            <li>Active l’API <b style="color:var(--ink)">Gmail API</b> (Bibliothèque).</li>
            <li>Écran de consentement OAuth : type <b style="color:var(--ink)">Externe</b>, ajoute ton adresse en <b style="color:var(--ink)">utilisateur test</b>.</li>
            <li>Crée un identifiant <b style="color:var(--ink)">ID client OAuth → Application Web</b>, avec cette URI de redirection :<br><code style="font-size:12px;background:var(--card);padding:2px 6px;border-radius:6px">${esc(g.redirect_uri)}</code></li>
            <li>Copie l’ID et le secret dans le fichier <code>.env</code> du projet :<br><code style="font-size:12px;background:var(--card);padding:2px 6px;border-radius:6px">GOOGLE_CLIENT_ID=…<br>GOOGLE_CLIENT_SECRET=…</code><br>puis relance le serveur.</li>
          </ol>`}
      </section>

      <section class="card-white" style="padding:24px 26px">
        <h3>Envois</h3>
        <div class="muted" style="font-size:13px;margin:4px 0 16px">Plafonds et mode de fonctionnement.</div>
        <div class="recap" style="box-shadow:none;background:var(--card);margin-bottom:16px">
          <div><span>Plafond mensuel</span><b>${s.caps.monthly} mails</b></div>
          <div><span>Plafond quotidien</span><b>${s.caps.daily} mails</b></div>
          <div><span>Rédaction Claude</span><b>${s.claude ? '<span style="color:var(--biz-ink)">active</span>' : '<span style="color:var(--ux-ink)">clé absente → par règles</span>'}</b></div>
          <div><span>Suivi des ouvertures</span><b>${s.pixel ? '<span style="color:var(--biz-ink)">actif</span>' : '<span style="color:var(--ux-ink)">inactif (pas d’URL publique)</span>'}</b></div>
        </div>
        <label class="check"><button class="switch ${s.dry_run ? 'on' : ''}" id="dry" type="button" ${s.dry_run_forced ? 'disabled' : ''}></button>
          <span><b style="color:var(--ink)">Mode simulation</b> — toute la chaîne tourne, aucun mail ne part${s.dry_run_forced ? ' (forcé par CR_DRY_RUN)' : ''}</span></label>
        <div class="hint" style="margin-top:12px">Les plafonds, la clé Claude et l’URL publique se règlent dans <code>.env</code> (voir <code>.env.example</code>).</div>
      </section>

      <section class="card-white" style="padding:24px 26px">
        <h3>Expéditeur</h3>
        <div class="muted" style="font-size:13px;margin:4px 0 16px">Ce que voient les recruteurs, et ce que Claude sait de toi pour rédiger.</div>
        <div class="field" style="margin-bottom:12px"><label class="lbl">Ton nom</label><input class="input on-paper" id="stName" value="${esc(v.sender_name)}" placeholder="Prénom Nom"></div>
        <div class="field" style="margin-bottom:12px"><label class="lbl">Signature</label><textarea class="input on-paper" id="stSig" style="min-height:90px" placeholder="Prénom Nom&#10;06 …&#10;linkedin.com/in/…">${esc(v.signature)}</textarea></div>
        <div class="grid-3" style="grid-template-columns:1fr 1fr;margin-bottom:12px">
          <div class="field"><label class="lbl">LinkedIn</label><input class="input on-paper" id="stLinkedin" value="${esc(v.linkedin_url)}" placeholder="linkedin.com/in/…"></div>
          <div class="field"><label class="lbl">Portfolio</label><input class="input on-paper" id="stPortfolio" value="${esc(v.portfolio_url)}" placeholder="https://…"></div>
        </div>
        <div class="hint" style="margin:-4px 0 12px">Insérés dans les mails par <b>{liens}</b> (ou séparément par {linkedin} et {portfolio}), cliquables.</div>
        <div class="field" style="margin-bottom:14px"><label class="lbl">Ton profil en quelques lignes</label><textarea class="input on-paper" id="stProfile" style="min-height:120px" placeholder="Ex. Développeur Python, 2 ans d’expérience en back-end (FastAPI, PostgreSQL), à l’aise en data. Cherche un poste à Bordeaux dans une équipe produit.">${esc(v.profile_summary)}</textarea>
          <div class="hint">Sert uniquement à la rédaction du paragraphe personnalisé. À défaut, le texte du CV est utilisé.</div></div>
        <button class="btn btn-primary" id="stSave">Enregistrer</button>
      </section>

      <section class="card-white" style="padding:24px 26px">
        <h3>CV</h3>
        <div class="muted" style="font-size:13px;margin:4px 0 16px">Joint à chaque envoi. PDF uniquement.</div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          ${s.cv_name ? `<span class="mono biz sm">✓</span><div><b>${esc(s.cv_name)}</b><div class="muted" style="font-size:12.5px">importé</div></div>` : `<span class="mono sm">PDF</span><div class="muted">Aucun CV importé</div>`}
          <span style="flex:1"></span>
          <label class="btn btn-white btn-sm" style="cursor:pointer">📎 ${s.cv_name ? 'Remplacer' : 'Importer'}<input type="file" id="stCv" accept="application/pdf" class="hidden"></label>
        </div>
      </section>
    </div>

    <section class="section">
      <div class="section-head"><h2><span class="emoji">✍️</span>Ton modèle de mail</h2><span class="muted" style="font-size:13px">Le point de départ de chaque campagne — tu peux encore l’ajuster campagne par campagne.</span></div>
      <div class="card"><div class="card-body">
        <div class="field" style="margin-bottom:12px"><label class="lbl">Objet</label><input class="input on-paper" id="tplSubject" value="${esc(s.defaults.subject)}"></div>
        <div class="field" style="margin-bottom:10px"><label class="lbl">Corps</label><textarea class="input on-paper" id="tplBody" style="min-height:260px">${esc(s.defaults.body)}</textarea></div>
        <div class="hint" style="margin-bottom:8px">Clique un élément pour l’insérer là où est le curseur. Les éléments en couleur sont remplis pour chaque entreprise ; <b>{ouverture}</b> et <b>{accroche}</b> sont rédigés à chaque envoi.</div>
        <div class="pills" id="tplVars" style="margin-bottom:18px">${Object.entries(s.variables).map(([k, d]) => `<button class="pill" data-var="${k}" title="${esc(d)}" style="background:${['ouverture', 'accroche'].includes(k) ? 'var(--ux)' : 'var(--card)'}">{${k}}</button>`).join('')}</div>
        <div class="section-head" style="margin-top:4px"><h3>Exemple, avec une entreprise fictive</h3></div>
        <div class="mail-preview" id="tplPreview"></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;align-items:center">
          <button class="btn btn-accent" id="tplSave">Enregistrer le modèle</button>
          <button class="btn btn-text" id="tplReset">Revenir au modèle d’origine</button>
          <span class="hint" id="tplHint"></span>
        </div>
      </div></div>
    </section>`;
  bindTemplateEditor(s);

  const disc = $('#gmDisc'); if (disc) disc.onclick = async () => { if (!confirm('Déconnecter Gmail ?')) return; await api('/api/gmail/disconnect', { method: 'POST' }); toast('Déconnecté'); renderSettings(); };
  const test = $('#gmTest'); if (test) test.onclick = async () => {
    test.disabled = true; test.textContent = 'Rédaction et envoi…';
    try {
      const runs = await api('/api/runs');
      const r = await api('/api/gmail/test', { method: 'POST', body: JSON.stringify({ job_title: $('#gmTestJob').value, run_id: runs[0]?.id || null }) });
      toast(`Envoyé à ${r.to} — « ${r.subject} »`);
    } catch (e) { toast(e.message); }
    test.disabled = false; test.textContent = '✉ M’envoyer un mail de test';
  };
  $('#dry').onclick = async () => { const on = !$('#dry').classList.contains('on'); await api('/api/settings', { method: 'PUT', body: JSON.stringify({ dry_run: on }) }); $('#dry').classList.toggle('on', on); toast(on ? 'Simulation activée' : 'Envois réels activés'); state.settings = null; window.refreshBadges && window.refreshBadges(); };
  $('#stSave').onclick = async () => { await api('/api/settings', { method: 'PUT', body: JSON.stringify({ sender_name: $('#stName').value, signature: $('#stSig').value, profile_summary: $('#stProfile').value, linkedin_url: $('#stLinkedin').value, portfolio_url: $('#stPortfolio').value }) }); toast('Enregistré'); state.settings = null; };
  $('#stCv').onchange = async (e) => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append('file', f);
    try { await upload('/api/settings/cv', fd); toast('CV importé'); renderSettings(); } catch (err) { toast(err.message); } };
}
