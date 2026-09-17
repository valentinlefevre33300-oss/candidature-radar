/* Noyau partagé : utilitaires DOM, appels API, constantes, état, composants. */

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export async function api(url, opts = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!res.ok) { let d = ''; try { d = (await res.json()).detail; } catch {} throw new Error(typeof d === 'string' && d ? d : res.statusText); }
  return res.status === 204 ? null : res.json();
}
export async function upload(url, formData) {
  const res = await fetch(url, { method: 'POST', body: formData });
  if (!res.ok) { let d = ''; try { d = (await res.json()).detail; } catch {} throw new Error(d || res.statusText); }
  return res.json();
}

export const STATUS = { a_contacter: 'À contacter', contacte: 'Contacté', relance: 'Relancé', repondu: 'A répondu', ecarte: 'Écarté' };
export const CATEGORY = { metier: 'Métier', rh: 'RH', direction: 'Direction', nominatif: 'Nominatif', generique: 'Générique', technique: 'Technique', juridique: 'Juridique', commercial: 'Commercial', inconnu: 'Inconnu' };
export const contactTags = (c) => [
  c.is_manager && c.category === 'metier' ? '<span class="tag info" title="Dirige le service qui recrute pour ce poste">responsable</span>' : '',
  c.inferred ? (c.guessed || (c.pattern_used || '').endsWith('?')
    ? '<span class="tag danger" title="Aucune adresse nominative observée : motif prenom.nom supposé, rebond possible">motif supposé</span>'
    : '<span class="tag" title="Reconstituée depuis le motif maison, jamais vue en ligne">déduite</span>') : '',
  c.mx_ok === 0 ? '<span class="tag danger" title="Aucun serveur de messagerie déclaré">sans MX</span>' : '',
].join('');
export const APP_STATUS = { programme: 'Programmé', envoye: 'Envoyé', ouvert: 'Ouvert', repondu: 'A répondu', echec: 'Échec', annule: 'Annulé' };
export const CAMP_STATUS = { brouillon: 'Brouillon', active: 'Active', en_pause: 'En pause', terminee: 'Terminée' };
export const CAT_FAMILY = { metier: 'data', rh: 'biz', direction: 'tech', nominatif: 'ux' };
export const SECTOR_FAMILY = {
  tech: 'tech', data_ia: 'tech', ingenierie: 'tech', industrie: 'tech', energie: 'tech', btp: 'tech',
  conseil: 'biz', finance: 'biz', comptabilite: 'biz', rh: 'biz', commerce: 'biz', logistique: 'biz',
  immobilier: 'biz', agro: 'biz', hotellerie: 'biz', marketing: 'ux', design: 'ux', media: 'ux', formation: 'ux', sante: 'data',
};
export const FAMILY_INK = { tech: 'var(--tech-ink)', biz: 'var(--biz-ink)', ux: 'var(--ux-ink)', data: 'var(--data-ink)' };
export const HEADCOUNT = [
  { key: 'all', label: 'Toutes tailles', min: null, max: null },
  { key: 'tpe', label: '1 – 9', min: 1, max: 9 },
  { key: 'pme', label: '10 – 249', min: 10, max: 249 },
  { key: 'eti', label: '250 +', min: 250, max: null },
  { key: 'custom', label: 'Précis…', min: null, max: null },
];

export const state = {
  sectors: [],
  settings: null,
  form: { job: '', zone: '', head: 'pme', min: '', max: '', limit: 25, sectors: new Set(), keywords: '', useSearch: true, smtp: false },
  job: null,
  results: { filter: 'all', hideLow: true, q: '', open: new Set(), dismissedBanner: false },
  suivi: { filter: 'a_contacter' },
  wizard: null,
  campaign: { filter: '', q: '', page: 1, activity: false },
};

let toastTimer;
export function toast(msg) {
  const el = $('#toast'); el.textContent = msg; el.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
}

export function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  const t = d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return `aujourd'hui ${t}`;
  return d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'short' }) + ' ' + t;
}
export function fmtDay(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit', year: 'numeric' });
}
export function relTime(iso) {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'à l’instant';
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
  const d = Math.round(s / 86400);
  return d === 1 ? 'hier' : `il y a ${d} jours`;
}
export const sectorLabel = (key) => (state.sectors.find(s => s.key === key) || { label: key }).label.split(' / ')[0];

const NOISE = /^(sas|sasu|sarl|sa|eurl|sci|snc|scop|groupe|group|societe|société|ste|holding|cie|le|la|les|de|du|des|et)$/i;
export function monogram(name) {
  const words = String(name || '?').replace(/\(.*?\)/g, '').split(/[^A-Za-zÀ-ÿ0-9]+/).filter(w => w && !NOISE.test(w));
  const two = words.length >= 2 ? words[0][0] + words[1][0] : (words[0] || '?').slice(0, 2);
  return two.toUpperCase();
}
export function pretty(name) {
  const words = String(name || '').replace(/\(.*?\)/g, ' ').trim().split(/\s+/).filter(w => w && !NOISE.test(w));
  return words.map(w => (w === w.toUpperCase() && w.length <= 4) ? w : w[0].toUpperCase() + w.slice(1).toLowerCase()).join(' ') || name || '';
}

export function pageHead({ kicker, title, sub, right, back }) {
  return `<div class="page-head">
    ${back ? `<a href="${back.href}" class="muted" style="font-size:13px;display:inline-flex;align-items:center;gap:6px;margin-bottom:14px">← ${esc(back.label)}</a>` : ''}
    <div class="row">
      <div>${kicker ? `<p class="kicker">${kicker}</p>` : ''}<h1>${title}<span class="dot-accent">.</span></h1>
      ${sub ? `<div class="sub">${sub}</div>` : ''}</div>
      ${right ? `<div class="pills">${right}</div>` : ''}
    </div>
    <div class="rule"></div>
  </div>`;
}

/* Tilt 3D léger sur les lignes (comme les listes denses du Brief). Délégué au
   conteneur : les lignes sont recréées à chaque rendu. */
const TILT_OK = matchMedia('(hover: hover)').matches && !matchMedia('(prefers-reduced-motion: reduce)').matches;
export function enableTilt(container, deg = 4) {
  if (!TILT_OK || !container || container.dataset.tilt) return;
  container.dataset.tilt = '1';
  container.addEventListener('mousemove', (e) => {
    const el = e.target.closest('.item'); if (!el) return;
    const r = el.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
    el.style.transform = `perspective(900px) rotateX(${(-y * deg).toFixed(2)}deg) rotateY(${(x * deg).toFixed(2)}deg)`;
  });
  container.addEventListener('mouseout', (e) => {
    const el = e.target.closest('.item'); if (el && !el.contains(e.relatedTarget)) el.style.transform = '';
  });
}

/* Surligne les {variables} d'un gabarit, pour l'aperçu. */
export function highlightVars(text) {
  return esc(text).replace(/\{(\w+)\}/g, '<mark>{$1}</mark>');
}

export function openModal(html) {
  closeModal();
  const bg = document.createElement('div');
  bg.className = 'modal-bg'; bg.id = 'modalBg';
  bg.innerHTML = `<div class="modal">${html}</div>`;
  bg.addEventListener('click', (e) => { if (e.target === bg) closeModal(); });
  document.body.appendChild(bg);
  document.addEventListener('keydown', escClose);
}
export function closeModal() {
  const bg = $('#modalBg'); if (bg) bg.remove();
  document.removeEventListener('keydown', escClose);
}
function escClose(e) { if (e.key === 'Escape') closeModal(); }

export const linkedinSearch = (r) => 'https://www.linkedin.com/search/results/people/?keywords='
  + encodeURIComponent([r.first_name, r.last_name, pretty(r.company_name)].filter(Boolean).join(' '));

export const queryParam = (name) => new URLSearchParams((location.hash.split('?')[1] || '')).get(name);
