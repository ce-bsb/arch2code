/**
 * The stage track — five stages as one vertical trail, the current one open.
 *
 * This is the screen the product is actually about. Everything the pipeline did
 * lives here, grouped by the stage that did it, and nothing is more than one
 * click away:
 *
 *   stage  →  the model's reasoning, streaming
 *          →  every tool call, collapsed to one line each
 *          →  the files the stage wrote
 *          →  stderr, when there was any
 *
 * Three decisions worth stating:
 *
 * 1. ONE DOM NODE PER STAGE, PATCHED. Stages are built once and then updated in
 *    place. A run emits thousands of events; rebuilding the track per event
 *    would throw away the user's scroll position and every open <details>
 *    several times a second.
 *
 * 2. AUTO-EXPANSION YIELDS TO THE USER. The running stage opens by itself, but
 *    the moment somebody toggles a stage by hand that stage is theirs and the
 *    track stops touching it. Nothing is worse in a live demo than a panel that
 *    closes itself while you are reading it.
 *
 * 3. A STAGE THAT IS "running" WHILE THE RUN IS "awaiting_input" IS NOT RUNNING.
 *    The background task has exited and is waiting for a human. Showing a
 *    spinner there would claim the machine is working when nothing is.
 */

import {
  formatDuration,
  formatNumber,
  h,
  isPlainObject,
  replaceChildren,
  stageTitle,
} from '../util.js';
import { createReasoning, assistantText } from './reasoning.js';
import { createCallTracker, isCallEvent, renderToolCall } from './toolcall.js';

/** Stage ids that stop for a human. Mirrors StageSpec.is_gate in pipeline.py. */
const GATE_STAGES = new Set(['critic']);

export function createStageTrack(rootEl, { onOpenArtifact } = {}) {
  /** stageId -> {el, parts, reasoning, userToggled, callNodes} */
  const views = new Map();
  const tracker = createCallTracker();
  let stages = [];
  let runStatus = null;

  rootEl.classList.add('track');

  // -- building -------------------------------------------------------------

  function build(stage) {
    const marker = h('span', { class: 'stage__marker', 'aria-hidden': 'true' });
    const bodyId = `stage-body-${stage.id}`;

    const n = h('span', { class: 'stage__n' }, String(stage.index ?? '').padStart(2, '0'));
    const title = h('span', { class: 'stage__title' });
    const gateTag = GATE_STAGES.has(stage.id)
      ? h('span', { class: 'tag tag--sm tag--warm-gray' }, h('span', { class: 'tag__label' }, 'human gate'))
      : null;
    const facts = h('span', { class: 'stage__facts' });

    const head = h(
      'button',
      {
        type: 'button',
        class: 'stage__head',
        'aria-expanded': 'false',
        'aria-controls': bodyId,
        onclick: () => {
          const view = views.get(stage.id);
          view.userToggled = true;
          setOpen(stage.id, view.el.dataset.open !== 'true');
        },
      },
      n,
      title,
      gateTag,
      facts
    );

    const reasoningEl = h('div', { class: 'reasoning', 'aria-live': 'off' });
    const reasoning = createReasoning(reasoningEl);

    // The reasoning pane is capped so one chatty stage cannot push the other
    // four off the screen. The cap is a scroll box, not a truncation — but a
    // scroll box with no affordance reads as a bug, so it gets a real control.
    const expandBtn = h('button', {
      type: 'button',
      class: 'btn btn-sm btn-quiet',
      hidden: true,
      'aria-expanded': 'false',
      onclick: () => {
        const full = reasoningEl.classList.toggle('reasoning--full');
        expandBtn.setAttribute('aria-expanded', String(full));
        expandBtn.textContent = full ? 'Collapse the reasoning' : 'Read all of it';
      },
    }, 'Read all of it');
    const callsEl = h('div', { class: 'calls' });
    const callsLabel = h('p', { class: 'section-title' }, 'Tool calls');
    const callsSection = h('div', { class: 'stage__section', hidden: true }, callsLabel, callsEl);
    const wroteEl = h('div', { class: 'wrote' });
    const wroteSection = h('div', { class: 'stage__section', hidden: true },
      h('p', { class: 'section-title' }, 'Files written'), wroteEl);
    const extraEl = h('div', { class: 'stage__section', hidden: true });

    const body = h(
      'div',
      { class: 'stage__body', id: bodyId, hidden: true },
      h('div', { class: 'stage__section' },
        h('p', { class: 'section-title' }, 'Reasoning'),
        reasoningEl,
        expandBtn),
      callsSection,
      wroteSection,
      extraEl
    );

    const el = h('article', { class: 'stage', dataset: { state: 'pending', open: 'false' } },
      marker, head, body);

    return {
      el,
      parts: {
        head, title, facts, body, expandBtn,
        callsSection, callsLabel, callsEl, wroteEl, wroteSection, extraEl,
      },
      reasoning,
      userToggled: false,
      callNodes: new Map(),
    };
  }

  /** Roughly a screenful of prose. Below it, the cap never bites. */
  const REASONING_CAP = 1600;

  function syncExpand(view) {
    if (!view) return;
    view.parts.expandBtn.hidden = view.reasoning.length() < REASONING_CAP;
  }

  function setOpen(stageId, open) {
    const view = views.get(stageId);
    if (!view) return;
    view.el.dataset.open = String(open);
    view.parts.body.hidden = !open;
    view.parts.head.setAttribute('aria-expanded', String(open));
  }

  // -- painting -------------------------------------------------------------

  function paintStage(stage) {
    const view = views.get(stage.id);
    if (!view) return;
    const state = effectiveState(stage);
    view.el.dataset.state = state;
    view.parts.title.textContent = stage.title || stageTitle(stage.id);

    const stats = isPlainObject(stage.stats) ? stage.stats : {};
    const facts = [];
    if (state === 'running') {
      facts.push(h('span', { class: 'spinner' }, 'running'));
    } else if (state === 'awaiting') {
      // No badge here: the marker is amber and the head already carries a
      // "human gate" tag. A third mark saying the same thing is noise.
      facts.push(h('span', { class: 'meta' }, 'waiting for you'));
    } else if (state === 'failed') {
      facts.push(h('span', { class: 'status-badge status-failed' }, 'failed'));
    }
    // The stage failed but the run went on with a mechanically derived
    // artifact. Both conditions are checked because only the first survives a
    // live reducer and only the second survives a page reload: `fallback` is a
    // client-side flag set by the `run.stage.fallback` event, while the error
    // code is what run.json carries on disk.
    if (stage.fallback || (isPlainObject(stage.error) && stage.error.code === 'analyst_fallback_applied')) {
      facts.push(h('span', { class: 'tag tag--sm tag--magenta' },
        h('span', { class: 'tag__label' }, 'fallback')));
    }
    // Two attempts were paid for. `attempts` comes from run.json on a reload
    // and from the `run.stage.retry` event live, so the mark survives both.
    const attempts = Number(stage.attempts) || 1;
    if (attempts > 1) {
      facts.push(h('span', {
        class: 'tag tag--sm tag--warm-gray',
        title: stage.retry_reason
          || 'The first attempt failed upstream before producing anything and the stage was started again.',
      }, h('span', { class: 'tag__label' }, `attempt ${attempts}`)));
    }
    if (stage.duration_ms != null) facts.push(h('span', null, formatDuration(stage.duration_ms)));
    if (stats.total_tokens != null) facts.push(h('span', null, `${formatNumber(stats.total_tokens)} tok`));
    if (stage.exit_code != null && stage.exit_code !== 0) {
      facts.push(h('span', { class: 'problem' }, `exit ${stage.exit_code}`));
    }
    if (stage.empty_stdout) {
      facts.push(h('span', { class: 'tag tag--sm tag--red' },
        h('span', { class: 'tag__label' }, 'no stdout')));
    }
    replaceChildren(view.parts.facts, facts);

    view.reasoning.setLive(state === 'running');
    syncExpand(view);

    // Artifacts this stage wrote.
    const artifacts = Array.isArray(stage.artifacts) ? stage.artifacts : [];
    if (artifacts.length) {
      view.parts.wroteSection.hidden = false;
      replaceChildren(
        view.parts.wroteEl,
        artifacts.map((artifact) =>
          h('button', {
            type: 'button',
            class: artifact.exists === false ? 'is-missing' : null,
            title: artifact.exists === false
              ? 'The stage contracted to write this file and did not.'
              : 'Open this file',
            onclick: () => onOpenArtifact && onOpenArtifact(artifact),
          }, shortPath(artifact.rel_path || artifact.path || artifact.artifact_id)))
      );
    } else {
      view.parts.wroteSection.hidden = true;
    }

    // A failure explains itself where it happened, not in a banner far away.
    const extras = [];
    if (stage.error) {
      extras.push(
        h('div', { class: 'notice notice-error' },
          h('div', { class: 'notice__body' },
            h('p', { class: 'notice-title' }, stage.error.title || stage.error.code || 'This stage failed'),
            stage.error.detail ? h('p', null, stage.error.detail) : null,
            stage.error.remedy ? h('p', { class: 'notice-remedy' }, stage.error.remedy) : null))
      );
    }
    if (view.stderr) {
      extras.push(
        h('details', { class: 'disclose disclose--bare' },
          h('summary', null, 'stderr from this stage'),
          h('pre', { class: 'code-block code-stderr code-block--wrap' }, view.stderr))
      );
    }
    view.parts.extraEl.hidden = extras.length === 0;
    replaceChildren(view.parts.extraEl, extras);

    if (!view.userToggled) {
      const shouldOpen = state === 'running' || state === 'awaiting' || state === 'failed';
      setOpen(stage.id, shouldOpen);
    }
  }

  /**
   * The state the marker and the spinner should show, which is not always the
   * state the server recorded — see decision 3 in the module docstring.
   */
  function effectiveState(stage) {
    const status = stage.status || 'pending';
    if (runStatus === 'awaiting_input' && GATE_STAGES.has(stage.id)) return 'awaiting';
    if (status === 'running' && runStatus === 'awaiting_input') return 'awaiting';
    if (status === 'succeeded') return 'succeeded';
    if (status === 'failed' || status === 'blocked') return 'failed';
    if (status === 'running') return 'running';
    return 'pending';
  }

  function shortPath(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return parts.slice(-2).join('/') || String(path || 'file');
  }

  // -- public ---------------------------------------------------------------

  function setStages(nextStages, status) {
    stages = Array.isArray(nextStages) ? nextStages : [];
    runStatus = status || null;

    // Rebuild the list only when the set of stages changed. Reordering DOM on
    // every state tick would restart every CSS entrance animation.
    const wanted = stages.map((stage) => stage.id).join('|');
    if (rootEl.dataset.plan !== wanted) {
      rootEl.dataset.plan = wanted;
      for (const stage of stages) {
        if (!views.has(stage.id)) views.set(stage.id, build(stage));
      }
      replaceChildren(rootEl, stages.map((stage) => views.get(stage.id).el));
    }

    if (!stages.length) {
      replaceChildren(
        rootEl,
        h('p', { class: 'empty' }, 'The plan appears as soon as the run is created.')
      );
      return;
    }

    for (const stage of stages) paintStage(stage);
  }

  /**
   * Fold one event into the track.
   *
   * Assistant deltas go to the stage's reasoning buffer; tool events go to the
   * tracker and then to the stage's call list. Everything else is already
   * represented by the stage's own state and is deliberately not echoed here —
   * a second, chattier copy of the same information is what made the old
   * timeline unreadable.
   */
  function ingest(event) {
    if (!event || typeof event !== 'object') return;
    const stageId = event.stage || (event.data && event.data.stage) || null;
    const view = stageId ? views.get(stageId) : null;

    if (event.type === 'bob.message') {
      const text = assistantText(event);
      if (text && view) {
        view.reasoning.append(text);
        syncExpand(view);
      }
      return;
    }

    if (event.type === 'bob.stderr' && view) {
      const chunk = String((event.data && event.data.chunk) || '');
      view.stderr = clipTail((view.stderr || '') + chunk, 16000);
      return;
    }

    if (!isCallEvent(event.type)) return;
    const outcome = tracker.ingest(event);
    if (!outcome) return;

    const target = views.get(outcome.call.stage) || view;
    if (!target) return;

    if (outcome.created) {
      const node = renderToolCall(outcome.call);
      target.callNodes.set(outcome.call.key, node);
      target.parts.callsEl.appendChild(node);
      target.parts.callsSection.hidden = false;
      target.parts.callsLabel.textContent = `Tool calls · ${target.callNodes.size}`;
    } else {
      const node = target.callNodes.get(outcome.call.key);
      if (node && typeof node.update === 'function') node.update(outcome.call);
    }
  }

  function clear() {
    for (const view of views.values()) view.reasoning.destroy();
    views.clear();
    tracker.reset();
    stages = [];
    runStatus = null;
    delete rootEl.dataset.plan;
    replaceChildren(rootEl);
  }

  return {
    setStages,
    ingest,
    clear,
    /** Total tool calls seen, for the header meter. */
    callCount: () => tracker.calls().length,
    /** Open one stage programmatically — used when the gate wants attention. */
    open(stageId) {
      const view = views.get(stageId);
      if (!view) return;
      setOpen(stageId, true);
      view.el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    },
  };
}

function clipTail(text, limit) {
  return text.length <= limit ? text : text.slice(text.length - limit);
}
