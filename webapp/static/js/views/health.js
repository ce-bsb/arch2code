/**
 * Environment health: a chip in the header, a banner when something is wrong,
 * and a panel with every probe.
 *
 * The report blocks the two modes independently — a machine with a broken Bob
 * install can still run the whole vision preview — so a single global
 * "unhealthy" would be a lie. The chip is always present because a reviewer
 * needs to know at a glance whether what they are watching is trustworthy; the
 * banner appears only when there is something to act on, because a permanent
 * green bar is a bar people stop reading.
 *
 * Every failing probe shows its remedy as the primary text. A probe that fails
 * without one is a defect in the backend and this view says so out loud rather
 * than rendering an empty row.
 */

import { api } from '../api.js';
import { formatDateTime, h, replaceChildren } from '../util.js';

const LEVEL_ORDER = { error: 0, warn: 1, ok: 2 };
const DOT_CLASS = { ok: 'status-succeeded', warn: 'status-awaiting_input', error: 'status-failed' };

export function createHealthView(chipEl, bannerEl, panelEl, { onChange } = {}) {
  let report = null;
  let expanded = false;

  function setChip(level, text, title) {
    replaceChildren(
      chipEl,
      h('span', { class: `status-dot ${DOT_CLASS[level] || 'status-running'}`, 'aria-hidden': 'true' }),
      h('span', null, text)
    );
    chipEl.setAttribute('title', title);
    chipEl.setAttribute('aria-label', title);
  }

  async function load(refresh = false) {
    setChip('checking', refresh ? 'Re-checking…' : 'Checking…', 'Running the environment probes');
    chipEl.disabled = true;
    try {
      report = refresh ? await api.recheckHealth() : await api.health();
    } catch (err) {
      report = null;
      renderUnreachable(err);
      chipEl.disabled = false;
      if (onChange) onChange(null);
      return null;
    }
    chipEl.disabled = false;
    render();
    if (onChange) onChange(report);
    return report;
  }

  function renderUnreachable(err) {
    setChip('error', 'Server down', err.title || 'The local server is not answering');
    bannerEl.hidden = false;
    bannerEl.className = 'strip strip--error';
    replaceChildren(
      bannerEl,
      h('span', { class: 'status-dot status-failed', 'aria-hidden': 'true' }),
      h('div', null,
        h('strong', null, err.title || 'The local server is not answering'),
        h('span', null, ` ${err.detail || ''}`),
        h('span', { class: 'strip__remedy' }, err.remedy || 'Start it with ./run.sh from the webapp directory.')),
      h('button', { type: 'button', class: 'btn btn-sm btn-tertiary', onclick: () => load(false) }, 'Retry')
    );
    replaceChildren(panelEl, h('p', { class: 'empty' }, 'No health report — the server did not answer.'));
    panelEl.hidden = true;
    expanded = false;
    chipEl.setAttribute('aria-expanded', 'false');
  }

  function render() {
    if (!report) return;
    const probes = Array.isArray(report.probes) ? report.probes.slice() : [];
    probes.sort((a, b) => (LEVEL_ORDER[a.level] ?? 9) - (LEVEL_ORDER[b.level] ?? 9));

    const errors = probes.filter((p) => p.level === 'error');
    const warns = probes.filter((p) => p.level === 'warn');
    const blockedModes = new Set();
    for (const probe of probes) {
      if (probe.level === 'error') for (const mode of probe.blocks || []) blockedModes.add(mode);
    }

    const level = errors.length ? 'error' : warns.length ? 'warn' : 'ok';
    const okCount = probes.length - errors.length - warns.length;

    setChip(
      level,
      errors.length ? `${errors.length} failing` : warns.length ? `${warns.length} warning${warns.length === 1 ? '' : 's'}` : 'Environment ready',
      `${okCount} of ${probes.length} probes pass. Click for the full list.`
    );

    // The banner is for action only. Green needs no banner.
    if (level === 'ok') {
      bannerEl.hidden = true;
      replaceChildren(bannerEl);
    } else {
      bannerEl.hidden = false;
      bannerEl.className = `strip strip--${level === 'error' ? 'error' : 'warn'}`;
      const detail = errors.length
        ? blockedModes.size
          ? `${[...blockedModes].map(modeName).join(' and ')} ${blockedModes.size > 1 ? 'are' : 'is'} unavailable until this is fixed.`
          : 'Nothing is blocked, but the failures below are real.'
        : 'Both modes are available. Read the warnings before you trust a result.';
      replaceChildren(
        bannerEl,
        h('span', { class: `status-dot ${DOT_CLASS[level] || 'status-running'}`, 'aria-hidden': 'true' }),
        h('div', null,
          h('strong', null, errors.length
            ? `${errors.length} environment ${errors.length === 1 ? 'check' : 'checks'} failed`
            : `${warns.length} ${warns.length === 1 ? 'warning' : 'warnings'}`),
          h('span', null, ` ${detail}`),
          report.checked_at ? h('span', { class: 'meta' }, ` Checked ${formatDateTime(report.checked_at)}.`) : null),
        h('button', {
          type: 'button',
          class: 'btn btn-sm btn-tertiary',
          onclick: () => toggle(true),
        }, 'Show checks'),
        h('button', { type: 'button', class: 'btn btn-sm btn-quiet', onclick: () => load(true) }, 'Re-check')
      );
    }

    replaceChildren(
      panelEl,
      h('div', { class: 'row row--between' },
        h('h2', { class: 'section-title' }, `Environment · ${probes.length} probes`),
        h('div', { class: 'btn-set' },
          h('button', { type: 'button', class: 'btn btn-sm btn-quiet', onclick: () => load(true) }, 'Re-check'),
          h('button', { type: 'button', class: 'btn btn-sm btn-quiet', onclick: () => toggle(false) }, 'Close'))),
      h('ul', { class: 'probe-list' }, probes.map(renderProbe))
    );
    panelEl.hidden = !expanded;
    chipEl.setAttribute('aria-expanded', String(expanded));
  }

  function toggle(next) {
    expanded = next === undefined ? !expanded : Boolean(next);
    panelEl.hidden = !expanded;
    chipEl.setAttribute('aria-expanded', String(expanded));
    if (expanded) panelEl.focus();
    else chipEl.focus();
  }

  function renderProbe(probe) {
    return h(
      'li',
      { class: `probe probe-${probe.level}` },
      h('div', { class: 'probe-head' },
        h('span', { class: `probe-dot probe-dot-${probe.level}`, 'aria-hidden': 'true' }),
        h('span', { class: 'probe-title' }, probe.title || probe.id),
        h('code', { class: 'probe-id' }, probe.id),
        h('span', { class: 'visually-hidden' }, `status: ${probe.level}`),
        (probe.blocks || []).length
          ? h('span', { class: 'tag tag--sm tag--red' },
              h('span', { class: 'tag__label' }, `blocks ${probe.blocks.map(modeName).join(', ')}`))
          : null),
      probe.detail ? h('p', { class: 'probe-detail' }, probe.detail) : null,
      probe.level !== 'ok'
        ? probe.remedy
          ? h('p', { class: 'probe-remedy' }, probe.remedy)
          : h('p', { class: 'probe-remedy probe-remedy-missing' },
              'This probe failed without stating a remedy. Check the terminal running ./run.sh for the underlying error.')
        : null,
      probe.data && Object.keys(probe.data).length
        ? h('details', { class: 'disclose disclose--bare' },
            h('summary', null, 'Details'),
            h('pre', { class: 'code-block code-block--wrap' }, JSON.stringify(probe.data, null, 2)))
        : null
    );
  }

  chipEl.addEventListener('click', () => toggle());

  return {
    load,
    toggle,
    report: () => report,
    /** Probes that block a mode, so a view can explain exactly why it is disabled. */
    blockersFor(mode) {
      if (!report) return [];
      return (report.probes || []).filter((p) => p.level === 'error' && (p.blocks || []).includes(mode));
    },
    allows(mode) {
      if (!report) return false;
      return !(report.probes || []).some((p) => p.level === 'error' && (p.blocks || []).includes(mode));
    },
  };
}

function modeName(mode) {
  return mode === 'vision' ? 'Vision preview' : mode === 'pipeline' ? 'Full pipeline' : mode;
}

/** Inline block shown in place of a mode's controls when health forbids it. */
export function renderBlockedNotice(mode, probes) {
  return h(
    'div',
    { class: 'notice notice-error' },
    h('div', { class: 'notice__body' },
      h('p', { class: 'notice-title' }, `${modeName(mode)} is unavailable on this machine`),
      h('ul', { class: 'notice-list' },
        probes.map((probe) =>
          h('li', null,
            h('strong', null, probe.title || probe.id),
            probe.detail ? h('span', null, ` ${probe.detail}`) : null,
            probe.remedy ? h('p', { class: 'notice-remedy' }, probe.remedy) : null))),
      h('p', { class: 'notice-remedy' },
        'Fix the above, then press Re-check in the header. The server does not need a restart.'))
  );
}
