/**
 * Tool calls, as one collapsible block each.
 *
 * This is the product's value proposition rendered as a component: every call
 * made under the hood — the four `arch_vision_*` MCP tools, every `tool_use` /
 * `tool_result` pair inside Bob's stream-json, and every helper script the
 * pipeline shells out to — becomes ONE block carrying the tool name, the
 * parameters, the result, the duration and the token cost. Collapsed it is a
 * single line; expanded it shows the payload, and under that the untouched raw
 * frames.
 *
 * Two things make this harder than it looks, and both are handled here:
 *
 * 1. A call is TWO events separated in time. The tracker keeps a running block
 *    in the DOM and patches it in place when the result lands, so the timeline
 *    does not grow a second row and the user watches the call complete.
 *
 * 2. The server's convenience fields do not match the shapes Bob actually
 *    emits. Verified against tests/fixtures/bob_stream_ask.ndjson: a real
 *    `tool_use` line carries `tool_name`/`tool_id`/`parameters`, while
 *    ndjson.py looks for `name`/`id`/`input`; a real `tool_result` carries
 *    `status`/`output`/`tool_id` while it looks for `is_error`/`content`. So
 *    `data.tool_use_id`, `data.input_preview` and `data.is_error` come back
 *    null on live output. Every read below falls back to `data.payload`, which
 *    is exactly the escape hatch models.py documents: "the client is required
 *    to render raw whenever a convenience field comes back None".
 */

import {
  copyText,
  formatClock,
  formatDuration,
  formatNumber,
  h,
  isPlainObject,
  replaceChildren,
  safeStringify,
  truncate,
} from '../util.js';

/** Events that open, close, or single-handedly describe a call. */
const OPENS = new Set(['bob.tool_use', 'vision.capture.started', 'vision.extract.started']);
const CLOSES = new Set([
  'bob.tool_result',
  'vision.capture.finished',
  'vision.extract.finished',
  'vision.tool_error',
]);
const ONESHOT = new Set(['vision.verify.finished', 'script.finished']);

export function isCallEvent(type) {
  return OPENS.has(type) || CLOSES.has(type) || ONESHOT.has(type);
}

// ---------------------------------------------------------------------------
// tracker
// ---------------------------------------------------------------------------

/**
 * Folds the event stream into call records.
 *
 * `ingest(event)` returns `{call, created}` when the event belongs to a call,
 * and `null` when it does not — the caller renders a plain row in that case.
 */
export function createCallTracker() {
  const byKey = new Map();
  /** Open calls per correlation family, for the fallback pairing below. */
  const openStack = [];

  const order = [];

  function open(call) {
    byKey.set(call.key, call);
    order.push(call);
    // Only a call that is genuinely in flight belongs on the pairing stack. A
    // one-shot that arrives already finished must never absorb the next result.
    if (call.status === 'running') openStack.push(call);
    return { call, created: true };
  }

  function closeCall(call, patch) {
    const index = openStack.indexOf(call);
    if (index >= 0) openStack.splice(index, 1);
    Object.assign(call, patch);
    if (call.startedTs && call.endedTs && call.durationMs == null) {
      const delta = new Date(call.endedTs) - new Date(call.startedTs);
      if (Number.isFinite(delta) && delta >= 0) call.durationMs = delta;
    }
    return { call, created: false };
  }

  /**
   * Find the open call a result belongs to.
   * By id when the stream gives one; otherwise the most recent open call of the
   * same source in the same stage. Bob is single-threaded per session, so
   * last-in-first-matched is correct rather than merely convenient.
   */
  function findOpen(id, source, stage) {
    if (id) {
      const direct = byKey.get(`${source}:${id}`);
      if (direct && direct.status === 'running') return direct;
    }
    for (let i = openStack.length - 1; i >= 0; i -= 1) {
      const call = openStack[i];
      if (call.status !== 'running') continue;
      if (call.source === source && (!stage || call.stage === stage)) return call;
    }
    return null;
  }

  /**
   * The most recent call for a tool in a stage, finished or not.
   *
   * Needed because one piece of work can produce several events: the capture
   * step emits `vision.capture.started`, then `script.finished` for the
   * subprocess, then `vision.capture.finished` with the manifest. Those are one
   * call, not three, and rendering three blocks for one thing is exactly the
   * noise this component exists to remove.
   */
  function findByTool(tool, stage) {
    for (let i = order.length - 1; i >= 0; i -= 1) {
      const call = order[i];
      if (call.tool === tool && (!stage || call.stage === stage)) return call;
    }
    return null;
  }

  /** Merge a later event into a call that is already rendered. */
  function merge(call, patch) {
    const index = openStack.indexOf(call);
    if (index >= 0 && patch.status && patch.status !== 'running') openStack.splice(index, 1);
    for (const [key, value] of Object.entries(patch)) {
      if (value === null || value === undefined) continue;
      call[key] = value;
    }
    if (call.startedTs && call.endedTs && call.durationMs == null) {
      const delta = new Date(call.endedTs) - new Date(call.startedTs);
      if (Number.isFinite(delta) && delta >= 0) call.durationMs = delta;
    }
    return { call, created: false };
  }

  return {
    ingest(event) {
      const type = event.type;
      const data = isPlainObject(event.data) ? event.data : {};
      const payload = isPlainObject(data.payload) ? data.payload : {};
      const stage = event.stage || data.stage || null;

      // -- Bob: tool_use -----------------------------------------------------
      if (type === 'bob.tool_use') {
        const id = data.tool_use_id || payload.tool_id || payload.id || `ev${event.id}`;
        return open({
          key: `bob:${id}`,
          source: 'bob',
          sourceLabel: 'Bob',
          tool: data.tool || payload.tool_name || payload.name || 'unnamed tool',
          callId: id,
          stage,
          params: payload.parameters ?? payload.input ?? payload.arguments ?? data.input_preview ?? null,
          status: 'running',
          startedTs: event.ts,
          endedTs: null,
          durationMs: null,
          result: null,
          tokens: null,
          rawOpen: data.raw || safeStringify(event),
          rawClose: null,
        });
      }

      // -- Bob: tool_result --------------------------------------------------
      if (type === 'bob.tool_result') {
        const id = data.tool_use_id || payload.tool_id || payload.id || null;
        const call = findOpen(id, 'bob', stage);
        const status = payload.status || (data.is_error ? 'error' : 'success');
        const failed = data.is_error === true || String(status).toLowerCase() === 'error';
        const result = payload.output ?? payload.content ?? payload.result ?? data.output_preview ?? null;
        if (!call) {
          // A result with no matching call still has to be visible; a dropped
          // tool_use is itself a finding, so the block says so.
          return open({
            key: `bob:orphan:${event.id}`,
            source: 'bob',
            sourceLabel: 'Bob',
            tool: `${id || 'unknown'} (result without a matching tool_use)`,
            callId: id,
            stage,
            params: null,
            status: failed ? 'error' : 'ok',
            startedTs: event.ts,
            endedTs: event.ts,
            durationMs: null,
            result,
            tokens: null,
            rawOpen: null,
            rawClose: data.raw || safeStringify(event),
            orphan: true,
          });
        }
        return closeCall(call, {
          status: failed ? 'error' : 'ok',
          endedTs: event.ts,
          result,
          resultStatus: status,
          rawClose: data.raw || safeStringify(event),
        });
      }

      // -- MCP: capture ------------------------------------------------------
      if (type === 'vision.capture.started') {
        return open({
          key: `mcp:capture:${event.id}`,
          source: 'script',
          sourceLabel: 'Script',
          tool: 'capture_diagram.py',
          stage,
          params: { source_path: data.source_path, run_id: event.run_id },
          status: 'running',
          startedTs: event.ts,
          endedTs: null,
          durationMs: null,
          result: null,
          tokens: null,
          rawOpen: safeStringify(event),
          rawClose: null,
        });
      }
      if (type === 'vision.capture.finished') {
        const failed = data.exit_code != null && Number(data.exit_code) !== 0;
        const patch = {
          status: failed ? 'error' : 'ok',
          endedTs: event.ts,
          durationMs: data.duration_ms ?? null,
          result: data,
          rawClose: safeStringify(event),
        };
        const call = findByTool('capture_diagram.py', stage);
        if (!call) {
          return open({
            key: `mcp:capture:orphan:${event.id}`,
            source: 'script',
            sourceLabel: 'Script',
            tool: 'capture_diagram.py',
            stage,
            params: null,
            startedTs: event.ts,
            ...patch,
          });
        }
        return merge(call, patch);
      }

      // -- MCP: extract ------------------------------------------------------
      if (type === 'vision.extract.started') {
        return open({
          key: `mcp:extract:${event.id}`,
          source: 'mcp',
          sourceLabel: 'MCP',
          tool: data.tool || 'arch_vision_extract_architecture',
          stage,
          params: {
            image_path: data.image_path,
            source_kind: data.source_kind,
            hint: data.hint,
          },
          status: 'running',
          startedTs: event.ts,
          endedTs: null,
          durationMs: null,
          result: null,
          tokens: null,
          rawOpen: safeStringify(event),
          rawClose: null,
        });
      }
      if (type === 'vision.extract.finished') {
        const call = findOpen(null, 'mcp', stage);
        const patch = {
          status: 'ok',
          endedTs: event.ts,
          durationMs: data.duration_ms ?? null,
          result: data,
          rawClose: safeStringify(event),
        };
        if (!call) {
          return open({
            key: `mcp:extract:orphan:${event.id}`,
            source: 'mcp',
            sourceLabel: 'MCP',
            tool: 'arch_vision_extract_architecture',
            stage,
            params: null,
            startedTs: event.ts,
            ...patch,
          });
        }
        return closeCall(call, patch);
      }

      // -- MCP: a tool that returned an error payload ------------------------
      if (type === 'vision.tool_error') {
        const call = findOpen(null, 'mcp', stage) || findOpen(null, 'script', stage);
        const patch = {
          status: 'error',
          endedTs: event.ts,
          result: data.raw ?? null,
          message: data.message || null,
          remedy: data.remedy || null,
          rawClose: safeStringify(event),
        };
        if (!call) {
          return open({
            key: `mcp:error:${event.id}`,
            source: 'mcp',
            sourceLabel: 'MCP',
            tool: data.tool || 'arch_vision tool',
            stage,
            params: null,
            startedTs: event.ts,
            ...patch,
          });
        }
        return closeCall(call, patch);
      }

      // -- MCP: verify is request/response inside one endpoint ---------------
      if (type === 'vision.verify.finished') {
        return open({
          key: `mcp:verify:${event.id}`,
          source: 'mcp',
          sourceLabel: 'MCP',
          tool: 'arch_vision_verify_element',
          stage,
          params: { target_kind: data.target_kind, target_id: data.target_id, claim: data.claim },
          status: data.verdict === 'error' ? 'error' : 'ok',
          verdict: data.verdict || null,
          startedTs: event.ts,
          endedTs: event.ts,
          durationMs: data.duration_ms ?? null,
          result: data,
          tokens: null,
          rawOpen: null,
          rawClose: safeStringify(event),
        });
      }

      // -- helper scripts ----------------------------------------------------
      if (type === 'script.finished') {
        const failed = Number(data.exit_code) !== 0;
        const tool = data.script || 'script';
        const existing = findByTool(tool, stage);
        if (existing) {
          // The subprocess behind a call that is already on screen (capture is
          // the case that exists today). Enrich it; do not duplicate it.
          return merge(existing, {
            status: failed ? 'error' : 'ok',
            endedTs: event.ts,
            durationMs: data.duration_ms ?? null,
            exitCode: data.exit_code ?? null,
            stderr: data.stderr_tail || null,
            params: Array.isArray(data.argv) ? { argv: data.argv } : null,
            rawClose: safeStringify(event),
          });
        }
        return open({
          key: `script:${event.id}`,
          source: 'script',
          sourceLabel: 'Script',
          tool: data.script || 'script',
          stage,
          params: Array.isArray(data.argv) ? { argv: data.argv } : null,
          status: failed ? 'error' : 'ok',
          startedTs: event.ts,
          endedTs: event.ts,
          durationMs: data.duration_ms ?? null,
          result: data,
          exitCode: data.exit_code ?? null,
          stderr: data.stderr_tail || null,
          tokens: null,
          rawOpen: null,
          rawClose: safeStringify(event),
        });
      }

      return null;
    },

    /**
     * Attach the token/cost figures from a `bob.result` line to the stage's
     * calls. The stats are per SESSION, not per call — one Bob session is one
     * stage — so they are recorded on the stage, and the block shows them only
     * as the session total it is part of.
     */
    calls: () => order.slice(),
    get: (key) => byKey.get(key) || null,
    reset() {
      byKey.clear();
      openStack.length = 0;
      order.length = 0;
    },
  };
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

/**
 * Which tag colour a source gets. Blue is the accent of this design system and
 * is reserved for Bob itself; MCP and helper scripts take the two data hues.
 * Purple is deliberately absent — it is not in this project's palette.
 */
const SOURCE_TAG = { mcp: 'tag--cyan', bob: 'tag--blue', script: 'tag--teal' };

/**
 * One call as a `<details class="call">`.
 *
 * Collapsed: name, a one-line argument preview, status, duration. Expanded:
 * parameters, result, stderr, the correlation facts and the untouched raw
 * frames. The node carries an `update(call)` method so the tracker can patch a
 * running call in place — a call that grows a result must not jump to the
 * bottom of the list and lose the user's scroll position.
 */
export function renderToolCall(call) {
  const details = h('details', {
    class: ['call', call.status === 'error' && 'call--error'],
    dataset: { call: call.key },
  });
  const summary = h('summary');
  const body = h('div', { class: 'call__body' });
  details.append(summary, body);

  function paint(next) {
    Object.assign(call, next || {});
    details.classList.toggle('call--error', call.status === 'error');
    renderSummary(summary, call);
    // Only repaint an expanded body. A closed <details> is not visible, and
    // rebuilding it on every patch is work nobody sees.
    if (details.open || !body.firstChild) renderBody(body, call);
  }

  details.addEventListener('toggle', () => {
    if (details.open) renderBody(body, call);
  });

  paint(null);
  details.update = paint;
  return details;
}

function renderSummary(summary, call) {
  replaceChildren(
    summary,
    h('span', { class: 'call__caret', 'aria-hidden': 'true' }),
    h('span', { class: ['tag', 'tag--sm', SOURCE_TAG[call.source] || 'tag--cool-gray'] },
      h('span', { class: 'tag__label' }, call.sourceLabel || call.source)),
    h('span', { class: 'call__name' }, call.tool),
    h('span', { class: 'call__args' }, argPreview(call)),
    h('span', { class: 'call__facts' },
      call.status === 'running' ? h('span', { class: 'spinner' }, 'running') : statusMark(call),
      call.durationMs != null ? h('span', null, formatDuration(call.durationMs)) : null,
      call.tokens && call.tokens.total != null
        ? h('span', null, `${formatNumber(call.tokens.total)} tok`)
        : null)
  );
}

/**
 * The one line that tells you what this call actually did, without expanding it.
 * A path, a command or an id beats `{"path": "...", "encoding": ...}` every time.
 */
function argPreview(call) {
  const params = call.params;
  if (params == null) return '';
  if (typeof params === 'string') return truncate(params.replace(/\s+/g, ' '), 120);
  if (Array.isArray(params)) return truncate(params.join(' '), 120);
  if (!isPlainObject(params)) return truncate(String(params), 120);

  for (const key of ['path', 'file_path', 'command', 'image_path', 'source_path', 'query', 'target_id']) {
    const value = params[key];
    if (typeof value === 'string' && value) return truncate(value.replace(/\s+/g, ' '), 120);
  }
  if (Array.isArray(params.argv)) return truncate(params.argv.join(' '), 120);

  const first = Object.entries(params).find(([, value]) => typeof value === 'string' && value);
  if (first) return truncate(`${first[0]}: ${first[1].replace(/\s+/g, ' ')}`, 120);
  return truncate(safeStringify(params, 0), 120);
}

function statusMark(call) {
  if (call.status === 'error') {
    return h('span', { class: 'tag tag--sm tag--red' }, h('span', { class: 'tag__label' }, 'error'));
  }
  if (call.verdict) {
    return h('span', { class: `tag tag--sm ${verdictTag(call.verdict)}` },
      h('span', { class: 'tag__label' }, String(call.verdict).toUpperCase()));
  }
  return h('span', { class: 'tag tag--sm tag--green' }, h('span', { class: 'tag__label' }, 'ok'));
}

function verdictTag(verdict) {
  if (verdict === 'true') return 'tag--green';
  if (verdict === 'false' || verdict === 'error') return 'tag--red';
  return 'tag--warm-gray';
}

function renderBody(body, call) {
  body.textContent = '';

  if (call.message) body.appendChild(h('p', { class: 'problem' }, call.message));
  if (call.remedy) body.appendChild(h('p', { class: 'remedy' }, call.remedy));
  if (call.orphan) {
    body.appendChild(
      h('p', { class: 'problem' },
        'No matching tool_use was seen for this result. Either the stream dropped a line or the ' +
        'tool ids do not correlate — read the raw frame below before trusting the pairing.')
    );
  }

  body.appendChild(payloadSection('Parameters', call.params, 'This call carried no parameters.'));

  if (call.status === 'running') {
    body.appendChild(
      h('p', { class: 'dim' }, 'Waiting for the result. Nothing has come back for this call yet.')
    );
  } else {
    body.appendChild(payloadSection('Result', call.result, 'The call returned no payload.'));
  }

  if (call.stderr) {
    body.appendChild(
      h('div', { class: 'payload' },
        h('p', { class: 'payload__label' }, 'stderr'),
        h('pre', { class: 'code-block code-stderr code-block--wrap' }, call.stderr))
    );
  }

  const facts = [
    call.callId ? `id ${call.callId}` : null,
    call.exitCode != null ? `exit ${call.exitCode}` : null,
    call.resultStatus ? `status ${call.resultStatus}` : null,
    call.stage ? `stage ${call.stage}` : null,
    call.startedTs ? `started ${formatClock(call.startedTs)}` : null,
    call.durationMs != null ? formatDuration(call.durationMs) : null,
  ].filter(Boolean);
  if (facts.length) body.appendChild(h('p', { class: 'dim' }, facts.join('  ·  ')));

  const raw = [call.rawOpen, call.rawClose].filter(Boolean).join('\n');
  if (raw) {
    body.appendChild(
      h('details', { class: 'disclose disclose--bare' },
        h('summary', null, 'Raw stream frames'),
        h('div', { class: 'snippet' },
          h('div', { class: 'snippet__actions' },
            h('button', {
              type: 'button',
              class: 'btn btn-sm btn-quiet',
              onclick: async (event) => {
                const ok = await copyText(raw);
                const btn = event.currentTarget;
                btn.textContent = ok ? 'Copied' : 'Copy failed';
                setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
              },
            }, 'Copy')),
          h('pre', { class: 'code-block code-block--wrap' }, raw)))
    );
  }
}

function payloadSection(title, value, emptyText) {
  const wrap = h('div', { class: 'payload' }, h('p', { class: 'payload__label' }, title));
  if (value == null || value === '') {
    wrap.appendChild(h('p', { class: 'dim' }, emptyText));
    return wrap;
  }
  const text = typeof value === 'string' ? value : safeStringify(value);
  wrap.appendChild(h('pre', { class: 'code-block code-block--wrap' }, text));
  return wrap;
}
