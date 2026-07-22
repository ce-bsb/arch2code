/**
 * Application bootstrap and router.
 *
 * Two screens, addressed by the URL fragment:
 *   #/            the invitation — drop a drawing, or pick up an old run
 *   #/run/<id>    the run workspace: execution, the gate, delivery
 *
 * The fragment is the router because it costs nothing and buys three things
 * that matter in a live demo: the browser's back button works, a run is a link
 * somebody can paste into a chat, and a refresh in the middle of a five-minute
 * pipeline comes back to the same place — the SSE stream replays from
 * Last-Event-ID, so nothing is lost.
 *
 * This file owns the shell and the wiring between the screens. No business
 * logic lives here.
 */

import { api } from './api.js';
import { mountThemeToggle } from './theme.js';
import { h, replaceChildren } from './util.js';
import { createHealthView } from './views/health.js';
import { createLandingView } from './views/landing.js';
import { createRunView } from './views/run.js';

const els = {
  healthChip: document.getElementById('health-chip'),
  healthBanner: document.getElementById('health-banner'),
  healthPanel: document.getElementById('health-panel'),
  themeToggle: document.getElementById('theme-toggle'),
  landing: document.getElementById('screen-landing'),
  run: document.getElementById('screen-run'),
};

mountThemeToggle(els.themeToggle);

const health = createHealthView(els.healthChip, els.healthBanner, els.healthPanel, {
  onChange: () => landing && landing.refresh(),
});

const runView = createRunView(els.run, {
  onRunChanged: () => landing && landing.refresh(),
  onBack: () => navigate('#/'),
});

const landing = createLandingView(els.landing, {
  health,
  onOpenRun: (runId) => navigate(`#/run/${encodeURIComponent(runId)}`),
});

// -- router -------------------------------------------------------------------

function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function parse() {
  const raw = String(location.hash || '').replace(/^#/, '');
  const match = raw.match(/^\/run\/([^/?]+)/);
  if (match) return { screen: 'run', runId: decodeURIComponent(match[1]) };
  return { screen: 'landing', runId: null };
}

let currentRunId = null;

async function route() {
  const target = parse();

  if (target.screen === 'run') {
    els.landing.hidden = true;
    els.run.hidden = false;
    if (target.runId !== currentRunId) {
      currentRunId = target.runId;
      await runView.load(target.runId);
    }
    document.title = `${target.runId} — arch2code`;
    return;
  }

  // Leaving the run screen closes its stream: an EventSource left open on a
  // hidden screen keeps a server connection and keeps reducing events nobody
  // is looking at.
  runView.detach();
  currentRunId = null;
  els.run.hidden = true;
  els.landing.hidden = false;
  document.title = 'arch2code — from a drawing to a working solution';
  landing.refresh();
}

window.addEventListener('hashchange', route);
window.addEventListener('beforeunload', () => runView.detach());

// -- keyboard -----------------------------------------------------------------

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  // Escape closes the health panel wherever focus happens to be. Nothing else
  // in this app is modal, so there is no ambiguity about what Escape means.
  if (!els.healthPanel.hidden) {
    event.preventDefault();
    health.toggle(false);
  }
});

// -- start --------------------------------------------------------------------

health.load(false);
route();

// A run created in another tab, or a pipeline that moved on while this tab sat
// on the landing screen, should not require a manual refresh. Cheap poll, and
// only while the landing screen is the one on screen.
setInterval(() => {
  if (!els.landing.hidden) landing.refresh();
}, 15000);

// Surface a hard boot failure instead of a blank page. A module that throws at
// import time leaves the DOM exactly as index.html shipped it, which looks like
// a working page that ignores every click.
window.addEventListener('error', (event) => {
  if (!event || !event.message) return;
  const banner = els.healthBanner;
  if (!banner || !banner.hidden) return;
  banner.hidden = false;
  banner.className = 'strip strip--error';
  replaceChildren(
    banner,
    h('span', { class: 'status-dot status-failed', 'aria-hidden': 'true' }),
    h('strong', null, 'The interface hit an unexpected error'),
    h('span', null, event.message),
    h('span', { class: 'strip__remedy' },
      'Reload the page. If it repeats, the browser console has the stack and the terminal ' +
      'running ./run.sh has the server side.')
  );
});

export { api };
