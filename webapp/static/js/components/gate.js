/**
 * The stage-3 human gate — screen 3 of the journey.
 *
 * When the critic blocks, this becomes the screen. Not a red error banner: a
 * blocked run is the system WORKING, and it deserves the same typographic
 * seriousness as the rest of the product. What it must do is answer three
 * questions without a click:
 *
 *   what did the critic contest?      the findings, parsed out of verdict.md
 *   on what evidence?                 the element ids and the reasoning
 *   what can I do about it?           three choices, each with its consequence
 *
 * Four rules enforced here, in the UI and not only in the server:
 *
 *  1. `absent` is NEVER rendered as an approval. A verdict.md with no gate line
 *     means the gate mechanism did not fire; that is a defect of the run, and
 *     this card says so in those words. Both historical runs in .arch/ produced
 *     exactly that and stage 4 ran anyway — the failure this card exists to
 *     make impossible to repeat.
 *  2. A decision that contradicts the parsed verdict is an OVERRIDE, is
 *     labelled as one, and cannot be submitted without a written reason.
 *  3. Nothing is preselected when the verdict is blocked or absent. The default
 *     has to be a deliberate keystroke, not a click-through.
 *  4. The findings are read from the FULL verdict.md when the artifact is
 *     reachable, not only from the server's excerpt. The excerpt is truncated
 *     mid-sentence by design, and deciding off a truncated document is exactly
 *     the sloppiness the gate is meant to prevent.
 */

import { api } from '../api.js';
import { asArray, h, replaceChildren } from '../util.js';

const CHOICE_LABELS = {
  approve: 'Approve — generate the code',
  block: 'Block — end the run here',
  send_back: 'Send back — re-run an earlier stage',
};

const CHOICE_HINTS = {
  approve: 'Stage 4 scaffolds from this architecture. Only approve what you have actually read.',
  block: 'Terminal, but not a failure: the gate worked. The audit trail under .arch/ is kept.',
  send_back: 'Re-runs from the stage you choose, with your reason attached as feedback.',
};

export function createGateCard(rootEl, { onDecide, onOpenArtifact } = {}) {
  let busy = false;
  let loadedFor = null;
  let verdictText = '';

  function clear() {
    replaceChildren(rootEl);
    rootEl.hidden = true;
  }

  function render(gate, { runStatus, runId } = {}) {
    if (!gate) return clear();
    if (runStatus !== 'awaiting_input') {
      if (gate.decided) return renderDecided(gate);
      return clear();
    }

    rootEl.hidden = false;
    const verdict = gate.verdict || 'absent';
    const choices = asArray(gate.choices).length ? asArray(gate.choices) : ['approve', 'block', 'send_back'];

    const findingsEl = h('div', { class: 'gate__findings' });
    const excerptEl = h('div', { class: 'stack' });

    paintEvidence(findingsEl, excerptEl, gate, verdictText);

    // Fetch the full verdict once per run so the findings are complete.
    if (runId && loadedFor !== runId && gate.verdict_artifact_id) {
      loadedFor = runId;
      api
        .fetchArtifactText(runId, gate.verdict_artifact_id)
        .then(({ text }) => {
          verdictText = text || '';
          paintEvidence(findingsEl, excerptEl, gate, verdictText);
        })
        .catch(() => {
          /* the excerpt is already rendered; a missing artifact is its own finding */
        });
    }

    replaceChildren(
      rootEl,
      h('section', { class: `gate gate--${verdict}`, role: 'region', 'aria-label': 'Human gate decision' },
        h('div', { class: 'gate__head' },
          h('p', { class: 'eyebrow eyebrow--rule' }, 'Stage 3 · the human gate'),
          h('h2', { class: 'gate__title' }, headline(verdict)),
          h('p', { class: 'lead' }, explainer(verdict)),
          h('span', { class: 'gate__verdict' }, gate.gate_line || verdictBadge(verdict))),
        h('div', { class: 'gate__grid' },
          h('div', { class: 'gate__evidence' },
            h('p', { class: 'section-title' }, 'What the critic contested'),
            findingsEl,
            excerptEl,
            gate.verdict_artifact_id && typeof onOpenArtifact === 'function'
              ? h('div', { class: 'form-actions' },
                  h('button', {
                    type: 'button',
                    class: 'btn btn-sm btn-tertiary',
                    onclick: () => onOpenArtifact(gate.verdict_artifact_id),
                  }, 'Open verdict.md in the file viewer'))
              : null),
          buildForm(gate, verdict, choices)))
    );

    // Land the keyboard on the decision, not at the top of the page.
    const first = rootEl.querySelector('input[type=radio]');
    if (first) first.focus({ preventScroll: true });
  }

  // -- evidence -------------------------------------------------------------

  function paintEvidence(findingsEl, excerptEl, gate, fullText) {
    const source = fullText || gate.verdict_excerpt || '';
    const findings = parseFindings(source);

    if (findings.length) {
      replaceChildren(findingsEl, findings.map(renderFinding));
    } else if (source) {
      replaceChildren(
        findingsEl,
        h('p', { class: 'dim' },
          'verdict.md carries no “SEVERITY:” headings, so there is nothing to itemise. Read it in ' +
          'full below before deciding.')
      );
    } else {
      replaceChildren(
        findingsEl,
        h('p', { class: 'problem' },
          'No verdict text was returned at all. Open the artifact list and check whether the critic ' +
          'stage wrote verdict.md — a gate with no document behind it cannot be decided honestly.')
      );
    }

    const questions = parseSection(source, /^##\s+Questions for the Human/im);
    const summary = parseSection(source, /^##\s+Executive Summary/im);

    replaceChildren(
      excerptEl,
      summary
        ? h('div', { class: 'stack stack--tight' },
            h('p', { class: 'section-title' }, 'Executive summary'),
            h('p', { class: 'prose' }, stripMarkup(summary)))
        : null,
      questions
        ? h('div', { class: 'stack stack--tight' },
            h('p', { class: 'section-title' }, 'Questions it needs answered'),
            h('pre', { class: 'gate__excerpt' }, questions))
        : null,
      source
        ? h('details', { class: 'disclose' },
            h('summary', null, fullText ? 'The full verdict.md' : 'The excerpt the server returned'),
            h('pre', { class: 'gate__excerpt' }, source))
        : null,
      gate.findings_count != null
        ? h('p', { class: 'meta' }, `${gate.findings_count} findings reported by the critic.`)
        : null
    );
  }

  // -- the decision form ----------------------------------------------------

  function buildForm(gate, verdict, choices) {
    const reasonField = h('textarea', {
      class: 'textarea',
      id: 'gate-reason',
      rows: '3',
      placeholder: 'Written to gate/decision.json alongside your choice.',
    });

    const resumeSelect = h('select', { class: 'select', id: 'gate-resume' },
      h('option', { value: 'analyst' }, 'analyst — rebuild the architecture from the extraction'),
      h('option', { value: 'critic' }, 'critic — review the same architecture again'));
    const resumeWrap = h('div', { class: 'field', hidden: true },
      h('label', { for: 'gate-resume' }, 'Resume from'), resumeSelect);

    const overrideNotice = h('p', { class: 'gate__override', hidden: true });
    const errorNotice = h('div', { class: 'notice notice-error', hidden: true, role: 'alert' });

    const submit = h('button',
      { type: 'submit', class: 'btn btn-primary btn--full btn--center', disabled: true },
      'Choose a decision');

    let picked = null;

    const radios = choices.map((choice) => {
      const input = h('input', {
        type: 'radio',
        name: 'gate-decision',
        value: choice,
        id: `gate-${choice}`,
        onchange: () => {
          picked = choice;
          resumeWrap.hidden = choice !== 'send_back';
          const override = isOverride(verdict, choice);
          overrideNotice.hidden = !override;
          overrideNotice.textContent = override ? overrideText(verdict, choice) : '';
          submit.disabled = false;
          submit.textContent = override
            ? `Override and ${choice.replace('_', ' ')}`
            : `Confirm — ${choice.replace('_', ' ')}`;
          submit.classList.toggle('btn-danger', override);
          submit.classList.toggle('btn-primary', !override);
        },
      });
      return h('label', { class: 'choice', for: `gate-${choice}` },
        input,
        h('span', null,
          h('span', { class: 'choice__label' }, CHOICE_LABELS[choice] || choice),
          h('span', { class: 'choice__hint' }, CHOICE_HINTS[choice] || '')));
    });

    return h('form', {
      class: 'gate__decision',
      onsubmit: async (event) => {
        event.preventDefault();
        if (busy || !picked) return;
        errorNotice.hidden = true;
        const reason = reasonField.value.trim();
        if (isOverride(verdict, picked) && !reason) {
          errorNotice.hidden = false;
          replaceChildren(errorNotice,
            h('div', { class: 'notice__body' },
              h('p', { class: 'notice-title' }, 'A reason is required'),
              h('p', null,
                'Your decision contradicts the verdict the critic produced. The override is recorded ' +
                'in gate/decision.json and needs to say why.')));
          reasonField.focus();
          return;
        }
        busy = true;
        submit.disabled = true;
        submit.textContent = 'Submitting…';
        try {
          await onDecide({
            decision: picked,
            reason: reason || null,
            resume_from: picked === 'send_back' ? resumeSelect.value : null,
          });
        } catch (err) {
          busy = false;
          submit.disabled = false;
          submit.textContent = 'Retry the decision';
          errorNotice.hidden = false;
          replaceChildren(errorNotice,
            h('div', { class: 'notice__body' },
              h('p', { class: 'notice-title' }, err.title || 'The decision was rejected'),
              h('p', null, err.detail || ''),
              err.remedy ? h('p', { class: 'notice-remedy' }, err.remedy) : null));
        }
      },
    },
      h('fieldset', { class: 'gate__choices' },
        h('legend', null, 'Your decision'),
        radios),
      h('div', { class: 'field' },
        h('label', { for: 'gate-reason' }, 'Reason'),
        reasonField,
        h('p', { class: 'field-hint' }, reasonHint(verdict))),
      resumeWrap,
      overrideNotice,
      errorNotice,
      submit);
  }

  // -- after the fact -------------------------------------------------------

  function renderDecided(gate) {
    rootEl.hidden = false;
    replaceChildren(
      rootEl,
      h('section', { class: 'gate gate--decided' },
        h('div', { class: 'gate__head' },
          h('p', { class: 'eyebrow eyebrow--quiet' }, 'Stage 3 · the human gate — decided'),
          h('h2', { class: 'gate__title' },
            `A human chose to ${String(gate.decision || '—').replace('_', ' ')}.`),
          gate.override
            ? h('p', { class: 'lead' },
                'That decision contradicted the critic. It was recorded as an override, with the ' +
                'reason below, in gate/decision.json.')
            : h('p', { class: 'lead' }, `The critic’s verdict was ${verdictBadge(gate.verdict)}.`),
          h('span', { class: 'gate__verdict' }, gate.gate_line || verdictBadge(gate.verdict)),
          gate.reason ? h('p', { class: 'prose' }, gate.reason) : null,
          gate.resume_from ? h('p', { class: 'meta' }, `Resumed from ${gate.resume_from}.`) : null))
    );
  }

  return {
    render,
    clear,
    /** Forget the fetched verdict when the view moves to another run. */
    reset() {
      loadedFor = null;
      verdictText = '';
    },
  };
}

// ---------------------------------------------------------------------------
// verdict.md parsing
// ---------------------------------------------------------------------------

/**
 * Pull `### SEVERITY: LEVEL - Title` blocks out of the critic's document.
 *
 * The critic writes prose, not JSON, so this is a READER and not a parser: it
 * recognises the one heading shape the review skill specifies and gives up
 * gracefully on anything else. A finding it fails to recognise is still in the
 * full document rendered below, which is what makes giving up safe.
 */
export function parseFindings(text) {
  const source = String(text || '');
  const pattern = /^#{2,4}\s*SEVERITY:\s*([A-Za-z]+)\s*[-–—]\s*(.+)$/gim;
  const findings = [];
  let match;

  while ((match = pattern.exec(source)) !== null) {
    const start = match.index + match[0].length;
    pattern.lastIndex = start;
    const rest = source.slice(start);
    const nextHeading = rest.search(/^#{2,4}\s/m);
    const block = nextHeading < 0 ? rest : rest.slice(0, nextHeading);

    findings.push({
      severity: match[1].toLowerCase(),
      title: match[2].trim(),
      element: firstCapture(block, /\*\*Element:\*\*\s*(.+)/i),
      wrong: firstCapture(block, /\*\*What is wrong:\*\*\s*\n?([\s\S]*?)(?:\n\*\*|$)/i),
      todo: firstCapture(block, /\*\*What to do:\*\*\s*\n?([\s\S]*?)(?:\n\*\*|$)/i),
    });
  }

  return findings;
}

function firstCapture(text, pattern) {
  const match = pattern.exec(text);
  return match ? match[1].trim() : null;
}

/** The body of one `## Heading` section — the summary and the questions. */
export function parseSection(text, headingPattern) {
  const source = String(text || '');
  const match = headingPattern.exec(source);
  if (!match) return null;
  const start = match.index + match[0].length;
  const rest = source.slice(start);
  const next = rest.search(/^##\s/m);
  const body = (next < 0 ? rest : rest.slice(0, next)).trim();
  return body || null;
}

function renderFinding(finding) {
  return h('div', { class: 'finding' },
    h('span', { class: `finding__sev finding__sev--${finding.severity}` }, finding.severity.toUpperCase()),
    h('div', { class: 'finding__body' },
      h('p', { class: 'finding__title' }, finding.title),
      finding.element ? h('p', { class: 'finding__text' }, `Element: ${stripMarkup(finding.element)}`) : null,
      finding.wrong ? h('p', { class: 'finding__text' }, stripMarkup(finding.wrong)) : null,
      finding.todo ? h('p', { class: 'remedy' }, stripMarkup(finding.todo)) : null));
}

/** Backticks and asterisks out; this goes into a text node, never into markup. */
function stripMarkup(value) {
  return String(value || '').replace(/[`*]/g, '').trim();
}

// ---------------------------------------------------------------------------
// copy
// ---------------------------------------------------------------------------

function isOverride(verdict, choice) {
  if (verdict === 'approved') return choice !== 'approve';
  if (verdict === 'blocked') return choice === 'approve';
  // absent: there is no machine verdict to contradict, but approving on the
  // strength of nothing is exactly the move that must be recorded.
  return choice === 'approve';
}

function overrideText(verdict, choice) {
  if (verdict === 'approved') {
    return `The critic approved this architecture and you are choosing to ${choice.replace('_', ' ')}. That is an override and it is recorded.`;
  }
  if (verdict === 'blocked') {
    return 'The critic BLOCKED this architecture and you are approving it anyway. This is the only place that override can happen, and it is written to gate/decision.json with your reason.';
  }
  return 'verdict.md carried no machine-readable decision, so approving here rests entirely on your own reading. That is recorded as an override.';
}

function reasonHint(verdict) {
  if (verdict === 'absent') {
    return 'The run produced no verdict line. Say what you read in verdict.md that justifies your decision.';
  }
  return 'Optional when you agree with the verdict, mandatory when you do not.';
}

function verdictBadge(verdict) {
  if (verdict === 'approved') return 'VERDICT: APPROVED';
  if (verdict === 'blocked') return 'VERDICT: BLOCKED';
  return 'NO VERDICT LINE';
}

function headline(verdict) {
  if (verdict === 'approved') return 'The critic approved this architecture.';
  if (verdict === 'blocked') return 'The critic blocked this architecture.';
  return 'This run produced no machine-readable verdict.';
}

function explainer(verdict) {
  if (verdict === 'approved') {
    return 'It wrote VERDICT: APPROVED as the last line of verdict.md. Read the findings before you agree — the gate is yours, not the model’s.';
  }
  if (verdict === 'blocked') {
    return 'It wrote VERDICT: BLOCKED. Nothing is generated until you decide. Approving from here overrides a machine decision and is recorded as such.';
  }
  return 'The last non-empty line of verdict.md is neither VERDICT: APPROVED nor VERDICT: BLOCKED, so the gate mechanism never fired. This is a defect of the run, not an approval by omission — read the document and decide explicitly.';
}
