/* Vue : Réglages — Gmail, expéditeur, CV, envois, rédaction. */
import { $, $$, esc, api, upload, state, toast, pageHead, queryParam } from './core.js';

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
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap"><span class="mono biz sm">✓</span><div><b>${esc(g.email || 'Compte connecté')}</b><div class="muted" style="font-size:12.5px">envoi + lecture des en-têtes</div></div>
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
      <div class="section-head"><h2><span class="emoji">✍️</span>Variables du gabarit</h2></div>
      <div class="card" style="padding:18px 22px"><div class="pills">${Object.entries(s.variables).map(([k, d]) => `<span class="pill" title="${esc(d)}" style="background:#fff;cursor:help">{${k}} <span class="muted" style="font-weight:400">— ${esc(d)}</span></span>`).join('')}</div></div>
    </section>`;

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
  $('#stSave').onclick = async () => { await api('/api/settings', { method: 'PUT', body: JSON.stringify({ sender_name: $('#stName').value, signature: $('#stSig').value, profile_summary: $('#stProfile').value }) }); toast('Enregistré'); state.settings = null; };
  $('#stCv').onchange = async (e) => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append('file', f);
    try { await upload('/api/settings/cv', fd); toast('CV importé'); renderSettings(); } catch (err) { toast(err.message); } };
}
