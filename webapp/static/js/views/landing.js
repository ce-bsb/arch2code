/**
 * Screen 1 — the invitation.
 *
 * A CEO who scribbled an idea on a napkin has to understand what to do here in
 * three seconds. So the screen holds exactly one instrument: a large drop zone.
 * There is no mode picker, no radio button, no visible field.
 *
 * ONE PATH
 * --------
 * There used to be two: "read the diagram" and "diagram to code". Nobody opens
 * this app to have a diagram described back to them. There is one journey —
 * drawing → complete solution — and the vision preview is a STAGE of it, not a
 * choice on the front door.
 *
 * ONE GESTURE
 * -----------
 * Dropping a file uploads it, creates the run and starts it. Create and start
 * stay two calls on the wire so a browser retry cannot pay twice, but they are
 * one gesture for the user, because the default intent is always to advance.
 *
 * The specialist knobs — source kind, slug, hint, PTY — live behind a collapsed
 * "Advanced options" disclosure with defaults that work. They are read at fire
 * time, so opening the disclosure first and dropping second does the right
 * thing, and never opening it at all does the right thing too.
 */

import { api } from '../api.js';
import { renderBlockedNotice } from './health.js';
import { ACCEPT, FORMAT_GROUPS } from '../formats.js';
import { formatDateTime, h, replaceChildren, stageTitle } from '../util.js';

const SOURCE_KINDS = [
  ['napkin', 'Napkin — hand-drawn, photographed'],
  ['whiteboard', 'Whiteboard — photographed, glare and angle'],
  ['screenshot', 'Screenshot — a rendered diagram or an export'],
  ['pdf', 'PDF page'],
];

/** The five stages, in the words a non-engineer reads them. */
const JOURNEY = [
  ['01', 'Intake', 'It looks at your drawing and writes down what it can actually see — with a confidence on every box.'],
  ['02', 'Analyst', 'It turns those observations into an architecture, separating what was drawn from what it inferred.'],
  ['03', 'Critic', 'A second pass argues against the first and blocks anything it cannot defend.'],
  ['04', 'Your gate', 'Nothing is generated until a human approves. Blocking is the system working.'],
  ['05', 'Scaffold', 'Code, generated from the approved architecture and validated before you get it.'],
];

export function createLandingView(rootEl, { health, onOpenRun } = {}) {
  let busy = false;

  // -- controls kept across renders so their values survive a refresh --------

  const sourceKind = h('select', { id: 'adv-source-kind', class: 'select' },
    SOURCE_KINDS.map(([value, label]) =>
      h('option', { value, selected: value === 'napkin' }, label)));

  const slugField = h('input', {
    type: 'text', id: 'adv-slug', class: 'input', maxlength: '24', pattern: '[a-z0-9-]*',
    placeholder: 'derived from the filename',
  });

  const hintField = h('input', {
    type: 'text', id: 'adv-hint', class: 'input', maxlength: '500',
    placeholder: 'e.g. “the boxes on the left are all one Kubernetes namespace”',
  });

  const ptyToggle = h('input', { type: 'checkbox', id: 'adv-pty' });

  const fileInput = h('input', {
    type: 'file',
    class: 'visually-hidden',
    id: 'a2c-file',
    accept: ACCEPT,
    onchange: (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) handleFile(file);
      event.target.value = '';
    },
  });

  // -- the drop zone --------------------------------------------------------

  const dzGlyph = h('span', { class: 'dz-glyph', 'aria-hidden': 'true' });
  const dzTitle = h('p', { class: 'dz-title' }, 'Drop your architecture drawing here');
  const dzHint = h('p', { class: 'dz-hint' }, 'or press Enter to choose a file');
  const dzMeta = h('p', { class: 'dz-meta' },
    'A napkin photo, a whiteboard shot, a screenshot, a PDF page or a .drawio file. ' +
    'Your original bytes are stored untouched; the model only ever reads a normalized copy.');
  const dzBar = h('span');
  const dzProgress = h('div', { class: 'dz-progress', hidden: true }, dzBar);

  const dropzone = h(
    'div',
    {
      class: 'dropzone',
      tabindex: '0',
      role: 'button',
      'aria-label': 'Upload an architecture drawing. Press Enter to browse, or drop a file here.',
      onclick: () => !busy && fileInput.click(),
      onkeydown: (event) => {
        if (busy) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          fileInput.click();
        }
      },
      ondragover: (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (!busy) dropzone.classList.add('is-over');
      },
      ondragleave: (event) => {
        event.stopPropagation();
        dropzone.classList.remove('is-over');
      },
      ondrop: (event) => {
        event.preventDefault();
        event.stopPropagation();
        dropzone.classList.remove('is-over');
        if (busy) return;
        const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
        if (file) handleFile(file);
      },
    },
    dzGlyph, dzTitle, dzHint, dzProgress, dzMeta
  );

  const errorEl = h('div', { hidden: true, role: 'alert' });
  const blockedEl = h('div', { hidden: true });
  const runsEl = h('div');

  replaceChildren(
    rootEl,
    h('div', { class: 'wrap wrap--narrow' },
      h('section', { class: 'invite' },
        h('div', { class: 'invite__lede' },
          h('p', { class: 'eyebrow eyebrow--rule' }, 'IBM BOB · arch2code'),
          h('h1', { class: 'hero-title' },
            'Turn an architecture drawing into a ',
            h('strong', null, 'working solution'),
            '.'),
          h('p', { class: 'lead' },
            'Upload a sketch. Five stages read it, argue with it and hand back reviewed code — ' +
            'and you watch every step of the reasoning as it happens.')),
        blockedEl,
        dropzone,
        fileInput,
        errorEl,
        advancedOptions(),
        h('div', { class: 'stack stack--tight' },
          h('p', { class: 'section-title' }, 'What happens next'),
          h('ol', { class: 'journey' },
            JOURNEY.map(([n, name, what]) =>
              h('li', { class: 'journey__step' },
                h('span', { class: 'journey__n' }, n),
                h('span', { class: 'journey__name' }, name),
                h('span', { class: 'journey__what' }, what))))),
        h('section', { class: 'surface', 'aria-label': 'Earlier runs' },
          h('div', { class: 'surface__head' },
            h('h2', { class: 'section-title' }, 'Earlier runs'),
            h('button', {
              type: 'button', class: 'btn btn-sm btn-quiet push',
              onclick: () => loadRuns(),
            }, 'Refresh')),
          h('div', { class: 'surface__body surface__body--flush' }, runsEl))))
  );

  function advancedOptions() {
    return h('details', { class: 'disclose' },
      h('summary', null, 'Advanced options'),
      h('div', { class: 'disclose__body' },
        h('p', { class: 'meta' },
          'Defaults that work are already set. Nothing here is required, and everything here is ' +
          'read at the moment you drop a file.'),
        h('div', { class: 'form-grid' },
          h('div', { class: 'field' },
            h('label', { for: 'adv-source-kind' }, 'How the drawing was made'),
            sourceKind,
            h('p', { class: 'field-hint' },
              'A hand-drawn kind makes the extractor more conservative: it prefers recording an ' +
              'unknown over guessing.')),
          h('div', { class: 'field' },
            h('label', { for: 'adv-slug' }, 'Run name'),
            slugField,
            h('p', { class: 'field-hint' },
              'Lowercase, digits and hyphens. Becomes YYYYMMDD-HHMM-<name>.')),
          h('div', { class: 'field' },
            h('label', { for: 'adv-hint' }, 'Hint for the model'),
            hintField,
            h('p', { class: 'field-hint' },
              'Context it is told to use — and told not to treat as something it saw on the page.'))),
        h('div', { class: 'field-inline' },
          ptyToggle,
          h('div', null,
            h('label', { for: 'adv-pty' }, 'Run the pipeline under a PTY'),
            h('p', { class: 'field-hint' },
              'At least one output path is TTY-conditioned. Leave this off unless a stage exits 0 ' +
              'with no output.'))),
        h('div', { class: 'stack stack--tight' },
          h('p', { class: 'section-title' }, 'Formats it accepts'),
          h('div', { class: 'form-grid' },
            FORMAT_GROUPS.map((group) =>
              h('div', { class: 'field' },
                h('p', { class: 'field-label' }, group.title),
                h('p', { class: 'meta mono' }, group.exts.join('  ')),
                h('p', { class: 'field-hint' }, group.hint))))),
        h('p', { class: 'field-hint' },
          'A run causes the pipeline to write its audit trail into this repository’s .arch/ tree. ' +
          'That is the pipeline producing evidence, not the app editing your code. Point ' +
          'ARCH2CODE_BOB_CWD at a scratch clone if you would rather it did not.')));
  }

  // -- the one gesture ------------------------------------------------------

  async function handleFile(file) {
    if (busy) return;

    if (health && !health.allows('pipeline')) {
      blockedEl.hidden = false;
      replaceChildren(blockedEl, renderBlockedNotice('pipeline', health.blockersFor('pipeline')));
      blockedEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      return;
    }

    busy = true;
    errorEl.hidden = true;
    blockedEl.hidden = true;
    dropzone.classList.add('is-busy');
    dzProgress.hidden = false;
    dzBar.style.width = '0%';
    dzTitle.textContent = `Uploading ${file.name}…`;
    dzHint.textContent = 'The run starts by itself the moment the file lands.';

    let run = null;
    try {
      const upload = await api.uploadFile(file, (fraction) => {
        dzBar.style.width = `${Math.round(fraction * 100)}%`;
      });

      dzTitle.textContent = 'Starting the pipeline…';
      dzHint.textContent = 'Stage 1 of 5.';

      run = await api.createRun({
        mode: 'pipeline',
        upload_id: upload.upload_id,
        slug: slugField.value.trim() || null,
        source_kind: sourceKind.value,
        hint: hintField.value.trim() || null,
        options: {
          use_pty: ptyToggle.checked ? true : null,
          max_coins: null,
          stage_timeout_s: null,
          auto_advance_gate: false,
        },
      });

      // Two calls on the wire, one gesture for the user.
      await api.startRun(run.run_id).catch(() => null);
      if (onOpenRun) onOpenRun(run.run_id);
    } catch (err) {
      replaceChildren(
        errorEl,
        h('div', { class: 'notice notice-error' },
          h('div', { class: 'notice__body' },
            h('p', { class: 'notice-title' }, err.title || 'That did not work'),
            err.detail ? h('p', null, err.detail) : null,
            h('p', { class: 'notice-remedy' },
              err.remedy || 'Try a different file, or open the environment chip in the header.')))
      );
      errorEl.hidden = false;
      // A run that was created but failed to start is still worth opening: its
      // own screen explains what went wrong far better than this one can.
      if (run && onOpenRun) onOpenRun(run.run_id);
    } finally {
      busy = false;
      dropzone.classList.remove('is-busy');
      dzProgress.hidden = true;
      dzTitle.textContent = 'Drop your architecture drawing here';
      dzHint.textContent = 'or press Enter to choose a file';
      loadRuns();
    }
  }

  // -- earlier runs ---------------------------------------------------------

  async function loadRuns() {
    let runs = [];
    try {
      const response = await api.listRuns({ limit: 12 });
      runs = (response && response.runs) || [];
    } catch (err) {
      replaceChildren(runsEl,
        h('div', { class: 'surface__body' },
          h('p', { class: 'empty' }, 'The run list is unavailable — the server did not answer.')));
      return;
    }

    if (!runs.length) {
      replaceChildren(runsEl,
        h('div', { class: 'surface__body' },
          h('p', { class: 'empty' }, 'Nothing has run yet. Drop a drawing above and this fills in.')));
      return;
    }

    replaceChildren(
      runsEl,
      h('div', { class: 'runlist' }, runs.map((run) =>
        h('button', {
          type: 'button',
          class: 'runlist__item',
          onclick: () => onOpenRun && onOpenRun(run.run_id),
        },
          h('span', { class: 'runlist__id' }, run.run_id),
          h('span', { class: 'runlist__src' }, run.source_filename || '—'),
          h('span', { class: `status-badge status-${run.status}` },
            String(run.status).replace('_', ' ')),
          h('span', { class: 'runlist__when' },
            `${run.stages_done ?? 0}/${run.stages_total ?? 0} · ${formatDateTime(run.updated_at || run.created_at)}`),
          run.current_stage
            ? h('span', { class: 'visually-hidden' }, `currently in ${stageTitle(run.current_stage)}`)
            : null)))
    );
  }

  loadRuns();

  return {
    refresh() {
      loadRuns();
      if (health && health.allows('pipeline')) blockedEl.hidden = true;
    },
  };
}
