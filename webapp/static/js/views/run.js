/**
 * The run workspace — screens 2, 3 and 4 of the journey, in one place.
 *
 *   EXECUTION    the drawing on the left, the five stages on the right, the
 *                current one open with its reasoning streaming and its tool
 *                calls one line each. Tokens, cost and elapsed time sit in the
 *                bar above, small.
 *   THE GATE     when the critic stops the run, the gate card is promoted above
 *                everything and the switcher is ignored. It is the only moment
 *                in the run that demands a human, and burying it behind a tab is
 *                how "absent" verdicts become silent approvals.
 *   DELIVERY     the file tree, the viewer with line numbers, and the two
 *                downloads. Reached by the switcher, or automatically when the
 *                run succeeds.
 *
 * WHY THERE IS NO GENERIC TIMELINE ANY MORE
 * -----------------------------------------
 * There used to be a flat log of every event next to the stage list. It showed
 * the same facts twice, in a less readable order, and it is what made the old
 * screen feel like a debugger. Everything it carried now lives inside the stage
 * that produced it. The raw frames are still one click away inside each call.
 */

import { api } from '../api.js';
import { createArtifactViewer, renderArtifactTree } from '../components/artifact.js';
import { createDiagramPanel } from '../components/diagram.js';
import { createGateCard } from '../components/gate.js';
import { createStageTrack } from '../components/stagetrack.js';
import { openRunStream } from '../sse.js';
import { createStore, hydrateRun, initialRunState, reduceEvent } from '../state.js';
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  formatNumber,
  h,
  replaceChildren,
  stageTitle,
} from '../util.js';

const STATUS_LABELS = {
  created: 'created — nothing has executed yet',
  running: 'running',
  awaiting_input: 'waiting for your gate decision',
  blocked: 'blocked at the gate',
  succeeded: 'succeeded',
  failed: 'failed',
  cancelled: 'cancelled',
};

export function createRunView(rootEl, { onRunChanged, onBack } = {}) {
  const store = createStore(initialRunState());
  let runId = null;
  let stream = null;
  let view = 'execution';
  let viewPinned = false;
  let artifacts = [];
  let selectedArtifactId = null;
  let elapsedTimer = null;

  // -- structure ------------------------------------------------------------

  const bannerEl = h('div', { class: 'strip', hidden: true, role: 'status' });
  const idEl = h('div', { class: 'runbar__id' });
  const statusEl = h('span');
  const metersEl = h('div', { class: 'meters' });
  const actionsEl = h('div', { class: 'btn-set' });
  const switcherEl = h('div', { class: 'switcher', role: 'tablist', 'aria-label': 'Run views' });
  const liveEl = h('p', { class: 'visually-hidden', role: 'status', 'aria-live': 'polite' });
  const gateEl = h('div', { hidden: true });

  const diagramEl = h('div', { class: 'workspace__left' });
  const trackEl = h('div');
  const execEl = h('div', { class: 'wrap' },
    h('div', { class: 'workspace' },
      diagramEl,
      h('div', { class: 'stack' },
        h('p', { class: 'section-title' }, 'What the model did, step by step'),
        trackEl)));

  const treeEl = h('div');
  const viewerEl = h('div', { class: 'surface viewer' });
  const downloadsEl = h('div', { class: 'downloads' });
  const deliverEl = h('div', { class: 'wrap', hidden: true },
    h('div', { class: 'deliver' },
      h('div', { class: 'stack stack--tight' },
        h('p', { class: 'eyebrow eyebrow--rule' }, 'Delivery'),
        h('h2', { class: 'section-heading' }, 'Everything this run produced')),
      downloadsEl,
      h('div', { class: 'files' },
        h('section', { class: 'surface', 'aria-label': 'Generated files' },
          h('div', { class: 'surface__head' }, h('h3', { class: 'section-title' }, 'Files')),
          h('div', { class: 'surface__body surface__body--flush' }, treeEl)),
        viewerEl)));

  const track = createStageTrack(trackEl, { onOpenArtifact: (artifact) => openArtifact(artifact) });
  const diagram = createDiagramPanel(diagramEl);
  const artifactViewer = createArtifactViewer(viewerEl, { runIdRef: () => runId });
  const gateCard = createGateCard(gateEl, {
    onDecide: submitGate,
    onOpenArtifact: openArtifactById,
  });

  buildSwitcher();

  replaceChildren(
    rootEl,
    bannerEl,
    h('div', { class: 'runbar' },
      h('button', { type: 'button', class: 'btn btn-sm btn-quiet', onclick: () => onBack && onBack() },
        '← All runs'),
      idEl,
      statusEl,
      switcherEl,
      metersEl,
      actionsEl),
    liveEl,
    gateEl,
    execEl,
    deliverEl
  );

  store.subscribe(() => {
    renderBar();
    renderMeters();
    renderTrack();
    renderGate();
    renderBanner();
  });

  // -- the switcher ---------------------------------------------------------

  function buildSwitcher() {
    const items = [
      ['execution', 'Execution'],
      ['delivery', 'Deliverables'],
    ];
    const buttons = items.map(([id, label]) =>
      h('button', {
        type: 'button',
        role: 'tab',
        id: `switch-${id}`,
        'aria-selected': String(view === id),
        onclick: () => {
          viewPinned = true;
          setView(id);
        },
      }, label));

    for (const button of buttons) {
      button.addEventListener('keydown', (event) => {
        const index = buttons.indexOf(button);
        let next = null;
        if (event.key === 'ArrowRight') next = buttons[(index + 1) % buttons.length];
        if (event.key === 'ArrowLeft') next = buttons[(index - 1 + buttons.length) % buttons.length];
        if (!next) return;
        event.preventDefault();
        next.focus();
        viewPinned = true;
        setView(next.id.replace('switch-', ''));
      });
    }
    replaceChildren(switcherEl, buttons);
  }

  function setView(next) {
    const changed = view !== next;
    view = next;
    for (const button of switcherEl.querySelectorAll('[role=tab]')) {
      button.setAttribute('aria-selected', String(button.id === `switch-${view}`));
    }
    execEl.hidden = view !== 'execution';
    deliverEl.hidden = view !== 'delivery';

    if (view === 'delivery') {
      // The gate owns the EXECUTION screen. On the delivery screen the user has
      // deliberately gone to look at files, and making them scroll past the
      // whole verdict to reach the download is not respect for the gate, it is
      // an obstacle. The switcher tab keeps the run's state visible.
      gateEl.hidden = true;
      refreshArtifacts().then(() => selectDefaultArtifact());
      refreshDownloads();
      return;
    }
    // Only on a real change: renderGate() can call back into setView, and an
    // unconditional call here is an infinite loop.
    if (changed) renderGate();
  }

  // -- loading --------------------------------------------------------------

  async function load(id) {
    detach();
    runId = id;
    artifacts = [];
    selectedArtifactId = null;
    view = 'execution';
    viewPinned = false;
    track.clear();
    diagram.clear();
    gateCard.reset();
    artifactViewer.showEmpty();
    hideBanner();
    replaceChildren(downloadsEl);
    store.setState(initialRunState());
    setView('execution');

    if (!runId) return;

    let run;
    try {
      run = await api.getRun(runId);
    } catch (err) {
      replaceChildren(
        idEl,
        h('div', { class: 'notice notice-error' },
          h('div', { class: 'notice__body' },
            h('p', { class: 'notice-title' }, err.title || 'Could not load this run'),
            h('p', null, err.detail || ''),
            err.remedy ? h('p', { class: 'notice-remedy' }, err.remedy) : null))
      );
      return;
    }

    store.setState((state) => hydrateRun(state, run));

    // Replay before tailing so a reload loses nothing. The stream also replays
    // from Last-Event-ID; sse.js dedupes on the monotonic id.
    try {
      const page = await api.replayEvents(runId, 0, 5000);
      for (const event of (page && page.events) || []) {
        store.setState((state) => reduceEvent(state, event));
        track.ingest(event);
      }
    } catch (err) {
      /* the run object is the source of truth for structure; the log is extra */
    }

    await Promise.all([refreshArtifacts(), diagram.load(run)]);
    refreshDownloads();
    startElapsed();

    // Only attach while the run can still produce events. A run parked at the
    // gate has NO task in flight — the background task exited and the log is
    // closed — so an EventSource there would reconnect forever and warn the user
    // about a stream that ended on purpose.
    if (isLive(run.status)) attachStream();
    else if (run.status === 'succeeded' && !viewPinned) setView('delivery');
  }

  function attachStream() {
    const after = store.getState().lastEventId;
    stream = openRunStream(runId, {
      after,
      onEvent: (event) => {
        store.setState((state) => reduceEvent(state, event));
        track.ingest(event);
        announce(event);
        sideEffects(event);
      },
      onOpen: () => {
        if (bannerEl.dataset.source === 'stream') hideBanner();
      },
      onError: (info) => {
        showBanner('warn', 'Lost the event stream', info.detail, null, 'stream');
      },
      onClose: () => {
        stream = null;
        if (bannerEl.dataset.source === 'stream') hideBanner();
      },
    });
  }

  function sideEffects(event) {
    if (event.type === 'artifact.written' || event.type === 'run.stage.finished') {
      refreshArtifacts();
      // The intake stage is what draws the boxes; reload the panel once it lands.
      if (event.stage === 'intake' || event.type === 'artifact.written') {
        const run = store.getState().run;
        if (run) diagram.load(run);
      }
    }
    if (['run.finished', 'run.failed', 'run.blocked', 'run.cancelled', 'run.awaiting_input'].includes(event.type)) {
      // The server closes the log on every one of these. Close our side too,
      // rather than letting EventSource retry against a finished run.
      detach();
      refreshArtifacts();
      refreshDownloads();
      if (onRunChanged) onRunChanged();
      api.getRun(runId)
        .then((run) => {
          store.setState((state) => hydrateRun(state, run));
          diagram.load(run);
          if (run.status === 'succeeded' && !viewPinned) setView('delivery');
        })
        .catch(() => {});
    }
  }

  /**
   * Only stage transitions and terminal states reach the polite live region.
   * Announcing every stream-json frame would make the app unusable with a
   * screen reader; announcing nothing would make it invisible.
   */
  function announce(event) {
    const data = event.data || {};
    if (event.type === 'run.stage.started') {
      liveEl.textContent = `${stageTitle(data.stage)} started.`;
    } else if (event.type === 'run.stage.finished') {
      liveEl.textContent = `${stageTitle(data.stage)} ${data.status || 'finished'}.`;
    } else if (event.type === 'run.awaiting_input') {
      liveEl.textContent = 'The run is waiting for your gate decision.';
    } else if (['run.finished', 'run.failed', 'run.blocked', 'run.cancelled'].includes(event.type)) {
      const run = store.getState().run;
      liveEl.textContent = `Run ${(run && run.status) || 'finished'}.`;
    }
  }

  function detach() {
    if (stream) {
      stream.close();
      stream = null;
    }
    stopElapsed();
  }

  // -- the bar --------------------------------------------------------------

  function renderBar() {
    const run = store.getState().run;
    if (!run) return;

    replaceChildren(
      idEl,
      h('h2', null, run.run_id),
      h('p', { class: 'meta' },
        [
          run.upload ? run.upload.filename : null,
          `${STATUS_LABELS[run.status] || run.status}`,
          `started ${formatDateTime(run.created_at)}`,
        ].filter(Boolean).join('  ·  '))
    );

    statusEl.className = `status-badge status-${run.status}`;
    statusEl.textContent = String(run.status).replace('_', ' ');

    const buttons = [];
    if (run.status === 'created') {
      buttons.push(h('button', {
        type: 'button', class: 'btn btn-sm btn-primary',
        onclick: async (event) => {
          const btn = event.currentTarget;
          btn.disabled = true;
          btn.textContent = 'Starting…';
          try {
            const updated = await api.startRun(run.run_id);
            store.setState((state) => hydrateRun(state, updated));
            attachStream();
            startElapsed();
          } catch (err) {
            btn.disabled = false;
            btn.textContent = 'Start';
            showBanner('error', err.title || 'Could not start', err.detail, err.remedy);
          }
        },
      }, 'Start'));
    }
    if (run.status === 'running') {
      buttons.push(h('button', {
        type: 'button', class: 'btn btn-sm btn-quiet',
        onclick: () => api.cancelRun(run.run_id).catch((err) =>
          showBanner('error', err.title || 'Could not cancel', err.detail, err.remedy)),
      }, 'Cancel'));
    }
    buttons.push(h('a', {
      class: 'btn btn-sm btn-tertiary',
      href: api.exportUrl(run.run_id),
      download: '',
    }, 'Download everything'));

    replaceChildren(actionsEl, buttons);
  }

  // -- meters ---------------------------------------------------------------

  function renderMeters() {
    const state = store.getState();
    const run = state.run;
    const totals = state.totals;
    const cost = state.cost || {};

    const tokens = (totals.tokens_in || 0) + (totals.tokens_out || 0);
    const items = [
      meter(tokens ? formatNumber(tokens) : '—', 'Tokens',
        tokens ? `${formatNumber(totals.tokens_in)} in · ${formatNumber(totals.tokens_out)} out` : 'reported at the end of each stage'),
      meter(cost.sessionCosts != null ? round(cost.sessionCosts) : '—', 'Cost',
        cost.maxBudget != null
          ? `budget ${round(cost.budgetSpend)} of ${round(cost.maxBudget)}`
          : 'Bobcoin spent across every stage of this run'),
      meter(formatDuration(elapsedMs(run)), 'Elapsed', 'wall clock since the run was created'),
      meter(formatNumber(track.callCount()), 'Tool calls', 'every call the pipeline made, all expandable below'),
    ];

    if (cost.maxBudget && cost.budgetSpend != null && cost.budgetSpend / cost.maxBudget >= 0.8) {
      items[1].classList.add('meter--warn');
    }
    replaceChildren(metersEl, items);
  }

  function meter(value, label, title) {
    return h('div', { class: 'meter', title },
      h('span', { class: 'meter__value' }, String(value)),
      h('span', { class: 'meter__label' }, label));
  }

  function elapsedMs(run) {
    if (!run || !run.created_at) return 0;
    const start = new Date(run.created_at).getTime();
    if (!Number.isFinite(start)) return 0;
    const end = isLive(run.status)
      ? Date.now()
      : new Date(run.updated_at || run.created_at).getTime();
    return Math.max(0, (Number.isFinite(end) ? end : Date.now()) - start);
  }

  /** One second is the right resolution: a faster tick is noise on a 5-minute run. */
  function startElapsed() {
    stopElapsed();
    const run = store.getState().run;
    if (!run || !isLive(run.status)) return;
    elapsedTimer = setInterval(renderMeters, 1000);
  }

  function stopElapsed() {
    if (elapsedTimer) clearInterval(elapsedTimer);
    elapsedTimer = null;
  }

  function round(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return String(Math.round(n * 100) / 100);
  }

  // -- track, gate, banner --------------------------------------------------

  function renderTrack() {
    const state = store.getState();
    track.setStages(state.stages, state.run && state.run.status);
  }

  function renderGate() {
    const state = store.getState();
    gateCard.render(state.gate, {
      runStatus: state.run && state.run.status,
      runId,
    });
    if (view === 'delivery') {
      gateEl.hidden = true;
      return;
    }
    // The gate owns the screen while it is open — but only pull the user back
    // to it if they are not already there, or setView calls us straight back.
    if (state.run && state.run.status === 'awaiting_input' && !viewPinned && view !== 'execution') {
      setView('execution');
    }
  }

  /**
   * Opening the delivery screen with an empty viewer wastes the one click that
   * matters. Prefer the document a reviewer actually came for — the verdict,
   * then the architecture — and fall back to whatever exists.
   */
  function selectDefaultArtifact() {
    if (selectedArtifactId || !artifacts.length) return;
    const present = artifacts.filter((artifact) => artifact.exists !== false);
    if (!present.length) return;
    const preferred =
      present.find((artifact) => /verdict\.md$/.test(String(artifact.rel_path || ''))) ||
      present.find((artifact) => /air\.json$/.test(String(artifact.rel_path || ''))) ||
      present[0];
    selectedArtifactId = preferred.artifact_id;
    replaceChildren(treeEl,
      renderArtifactTree(artifacts, { onSelect: openArtifact, selectedId: selectedArtifactId }));
    artifactViewer.open(preferred);
  }

  function renderBanner() {
    const state = store.getState();
    if (state.error) {
      // A failure that belongs to a stage is already rendered inside that
      // stage, with its remedy, exactly where it happened. Repeating it in a
      // strip two centimetres above says the same thing twice and trains people
      // to stop reading strips.
      const shownOnAStage = state.stages.some(
        (stage) => stage.error && stage.error.title === state.error.title
      );
      if (!shownOnAStage) {
        showBanner('error', state.error.title, state.error.detail, state.error.remedy);
        return;
      }
      if (bannerEl.dataset.source !== 'stream') hideBanner();
      return;
    }
    if (state.emptyStdoutStages.length) {
      showBanner('warn',
        `${state.emptyStdoutStages.length} stage(s) exited 0 with no output`,
        `Stages: ${state.emptyStdoutStages.join(', ')}. A silent success is not a success.`,
        'Re-run with “Run the pipeline under a PTY” switched on in Advanced options.');
      return;
    }
    if (state.missingArtifacts.length) {
      showBanner('warn',
        `${state.missingArtifacts.length} contracted file(s) were never written`,
        state.missingArtifacts.map((item) => item.expected_path).filter(Boolean).join(', '),
        state.missingArtifacts[0].remedy || 'Check the approval mode of the stage that should have written it.');
      return;
    }
    // Never step on the stream's banner: that one is owned by the SSE handlers
    // and is cleared when the connection comes back. Without the source tag a
    // stale banner from the PREVIOUS run survives into the next one.
    if (bannerEl.dataset.source !== 'stream') hideBanner();
  }

  function showBanner(level, title, detail, remedy, source = 'state') {
    bannerEl.hidden = false;
    bannerEl.className = `strip strip--${level === 'error' ? 'error' : 'warn'}`;
    bannerEl.dataset.source = source;
    replaceChildren(
      bannerEl,
      h('strong', null, title || 'Something went wrong'),
      detail ? h('span', null, detail) : null,
      remedy ? h('span', { class: 'strip__remedy' }, remedy) : null
    );
  }

  function hideBanner() {
    bannerEl.hidden = true;
    delete bannerEl.dataset.source;
    replaceChildren(bannerEl);
  }

  // -- gate submission ------------------------------------------------------

  async function submitGate(decision) {
    const updated = await api.decideGate(runId, decision);
    store.setState((state) => hydrateRun(state, updated));
    if (!stream && isLive(updated.status)) {
      attachStream();
      startElapsed();
    }
    if (onRunChanged) onRunChanged();
  }

  // -- delivery -------------------------------------------------------------

  async function refreshArtifacts() {
    if (!runId) return;
    try {
      const response = await api.listArtifacts(runId);
      artifacts = (response && response.artifacts) || [];
    } catch (err) {
      replaceChildren(treeEl,
        h('div', { class: 'surface__body' },
          h('p', { class: 'empty' }, 'The file list is unavailable — the server did not answer.')));
      return;
    }
    replaceChildren(treeEl,
      renderArtifactTree(artifacts, { onSelect: openArtifact, selectedId: selectedArtifactId }));
  }

  function openArtifact(artifact) {
    if (!artifact) return;
    selectedArtifactId = artifact.artifact_id;
    viewPinned = true;
    setView('delivery');
    replaceChildren(treeEl,
      renderArtifactTree(artifacts, { onSelect: openArtifact, selectedId: selectedArtifactId }));
    artifactViewer.open(artifact);
    viewerEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function openArtifactById(artifactId) {
    const found = artifacts.find((artifact) => artifact.artifact_id === artifactId);
    openArtifact(found || { artifact_id: artifactId, rel_path: artifactId, exists: true, bytes: 0 });
  }

  /**
   * The three downloads, each described by the planner that will build it.
   *
   * All three are always rendered. A download that cannot be produced says WHY,
   * in the server's own words, instead of vanishing — "there is no button" and
   * "the button is disabled because this run generated no code" are different
   * messages, and only one of them is true.
   */
  async function refreshDownloads() {
    if (!runId) return;
    const [full, code, project] = await Promise.all([
      api.exportPreview(runId, 'full'),
      api.exportPreview(runId, 'code'),
      api.exportPreview(runId, 'project'),
    ]);

    replaceChildren(
      downloadsEl,
      downloadCard({
        name: 'The whole solution',
        what:
          'Generated code, the full audit trail, both images and a MANIFEST.md that states which ' +
          'drawing this came from, which model read it, at what confidence, and what was left ' +
          'unresolved. This is the archive somebody opens months from now.',
        href: api.exportUrl(runId),
        preview: full,
        primary: true,
      }),
      downloadCard({
        name: 'Just the code',
        what:
          'Only what the scaffold stage generated, with no audit trail and no images — for ' +
          'somebody who wants to run it rather than review it.',
        href: api.exportCodeUrl(runId),
        preview: code,
        primary: false,
      }),
      downloadCard({
        name: 'The whole project tree',
        what:
          'Every file this run wrote anywhere in the project, at its real path — not only the ' +
          'build directory. MANIFEST.md states, per file, how it was attributed to this run and ' +
          'what that method cannot prove.',
        href: api.exportProjectUrl(runId),
        preview: project,
        primary: false,
      })
    );
  }

  function downloadCard({ name, what, href, preview, primary }) {
    // `project` is the count for the whole-tree archive, `code` for the other
    // two. Reading whichever the planner filled in keeps one card renderer for
    // all three instead of a per-kind branch.
    const counts = preview.counts || {};
    const sourceCount = counts.project ? counts.project : counts.code;
    const facts = preview.available
      ? h('p', { class: 'dl__facts' },
          h('span', null, `${formatNumber(preview.entry_count || 0)} files`),
          h('span', null, formatBytes(preview.total_bytes || 0)),
          sourceCount != null
            ? h('span', null, `${formatNumber(sourceCount)} source`)
            : null)
      : null;

    return h('div', { class: 'dl' },
      h('p', { class: 'dl__name' }, name),
      h('p', { class: 'dl__what' }, what),
      facts,
      preview.available
        ? h('a', {
            class: ['btn', primary ? 'btn-primary' : 'btn-tertiary', 'btn--full'],
            href,
            download: '',
          },
            `Download ${preview.filename ? '' : 'the archive'}`.trim() || 'Download',
            h('span', { class: 'btn__note' }, preview.filename || 'zip'))
        : h('div', { class: 'notice notice-warn' },
            h('div', { class: 'notice__body' },
              h('p', { class: 'notice-title' }, preview.title || 'Not available yet'),
              preview.detail ? h('p', null, preview.detail) : null,
              preview.remedy ? h('p', { class: 'notice-remedy' }, preview.remedy) : null)));
  }

  /** A run that can still emit events; everything else has no task in flight. */
  function isLive(status) {
    return status === 'created' || status === 'running';
  }

  return {
    load,
    detach,
    runId: () => runId,
    state: () => store.getState(),
  };
}
