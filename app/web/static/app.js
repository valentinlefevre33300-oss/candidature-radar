/* Point d'entrée : routeur, barre latérale, démarrage. */
import { $, $$, api, esc, state } from './core.js';
import { renderSearch, renderRun, renderSuivi } from './views-search.js';
import { renderDashboard as renderCampaigns, renderWizard, renderCampaign } from './views-campaigns.js';
import { renderSettings } from './views-settings.js';
import { renderDashboard } from './views-dashboard.js';

const ROUTES = [
  [/^#\/dashboard/, () => renderDashboard(), 'dashboard'],
  [/^#\/campagnes\/nouvelle/, () => renderWizard(), 'campagnes'],
  [/^#\/campagnes\/(\d+)/, (m) => renderCampaign(+m[1]), 'campagnes'],
  [/^#\/campagnes/, () => renderCampaigns(), 'campagnes'],
  [/^#\/recherche/, () => renderSearch(), 'recherche'],
  [/^#\/run\/(\d+)/, (m) => renderRun(+m[1]), 'recherche'],
  [/^#\/suivi/, () => renderSuivi(), 'suivi'],
  [/^#\/reglages/, () => renderSettings(), 'reglages'],
];

async function route() {
  const hash = location.hash || '#/campagnes';
  const hit = ROUTES.find(([re]) => re.test(hash)) || ROUTES[2];
  const m = hash.match(hit[0]) || [];
  $$('[data-nav]').forEach(a => a.classList.toggle('active', a.dataset.nav === hit[2]));
  window.scrollTo({ top: 0 });
  await hit[1](m);
}
window.addEventListener('hashchange', route);

async function refreshBadges() {
  try {
    const [rows, d] = await Promise.all([api('/api/outreach'), api('/api/dashboard')]);
    const n = rows.filter(r => r.status !== 'ecarte').length;
    $$('[data-badge="suivi"]').forEach(b => { b.textContent = n; b.classList.toggle('zero', !n); });
    const q = d.quota;
    $$('[data-badge="camp"]').forEach(b => { const a = d.campaigns.filter(c => c.status === 'active').length; b.textContent = a; b.classList.toggle('zero', !a); });
    const qm = $('#quotaMini'); if (qm) { qm.querySelector('span').textContent = `${q.month_sent}/${q.month_cap}`; qm.querySelector('i').style.width = `${Math.min(100, Math.round(100 * q.month_sent / q.month_cap))}%`; }
  } catch {}
}
window.refreshBadges = refreshBadges;

document.addEventListener('keydown', e => {
  if (e.key === '/' && !e.target.matches('input, textarea, select')) { const j = $('#job') || $('#wzJob'); if (j) { e.preventDefault(); j.focus(); } }
});

async function showAccount() {
  let me = null;
  try { me = await api('/api/me'); } catch { return; }
  const slot = $('#account');
  if (!me || !me.session || !slot) return;
  const initial = (me.name || me.email || '?').trim()[0].toUpperCase();
  const face = me.picture ? `<img src="${esc(me.picture)}" alt="" referrerpolicy="no-referrer">` : `<b>${esc(initial)}</b>`;
  slot.innerHTML = `<a href="/api/auth/logout" class="avatar" aria-label="Se déconnecter">${face}</a><span class="tip">${esc(me.email)} · se déconnecter</span>`;
  slot.hidden = false;
}

(async function boot() {
  try { state.sectors = await api('/api/sectors'); } catch {}
  refreshBadges();
  showAccount();
  route();
})();
