/**
 * The model's reasoning, rendered as readable prose while it streams.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * `bob.message` events with role=assistant are the reasoning. On the wire each
 * one is a DELTA — a few characters — wrapped in a JSON envelope. The previous
 * interface printed those envelopes verbatim, so the single most valuable thing
 * the product has to show (what the model was thinking) arrived as four thousand
 * lines of `{"type":"message","role":"assistant","content":"<thinking>\n",...}`.
 * Verified against a real 3 858-event run: 3 855 of those events are assistant
 * deltas averaging two characters each.
 *
 * So: concatenate the deltas per stage, then read the shape the model actually
 * emits, which is three interleaved things —
 *
 *   <thinking> … </thinking>            private reasoning
 *   free prose                          what it says out loud
 *   <execute_command><command>…</command></execute_command>
 *                                       a command it is about to run
 *
 * …plus `[using tool X: …]` markers the harness injects.
 *
 * RENDERING CONTRACT
 * ------------------
 * Everything is built as DOM nodes. Never innerHTML: this text is model output
 * derived from a user-supplied image and is treated as hostile. The only
 * inline formatting understood is **strong** and `code`, because that is what
 * the model actually uses in its own thinking headers.
 *
 * The re-render is whole-buffer and throttled to one animation frame. A partial
 * append would be faster in theory, but a `<thinking>` tag can arrive split
 * across two deltas, and a parser that has to survive that is a parser that
 * will get it wrong on stage 4 of a live demo.
 */

import { h, replaceChildren } from '../util.js';

/** Above this, the tail is what matters; the head is in the export. */
const MAX_CHARS = 200000;

export function createReasoning(rootEl, { live = false } = {}) {
  let buffer = '';
  let isLive = live;
  let frame = null;
  let dirty = false;

  function schedule() {
    dirty = true;
    if (frame != null) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      if (!dirty) return;
      dirty = false;
      paint();
    });
  }

  function paint() {
    const segments = parseReasoning(buffer);
    if (!segments.length) {
      replaceChildren(
        rootEl,
        h('p', { class: 'dim' },
          isLive
            ? 'The model has not said anything yet for this stage.'
            : 'This stage produced no assistant output. Either it was purely mechanical, or its ' +
              'stdout was not stream-json — check the tool calls below.')
      );
      return;
    }

    const nodes = segments.map((segment, index) =>
      renderSegment(segment, isLive && index === segments.length - 1)
    );
    replaceChildren(rootEl, nodes);
  }

  return {
    /** Append one delta. Cheap: the parse happens at most once per frame. */
    append(text) {
      if (!text) return;
      buffer += String(text);
      if (buffer.length > MAX_CHARS) buffer = buffer.slice(buffer.length - MAX_CHARS);
      schedule();
    },
    /** Replace the whole buffer — used when a stage is re-rendered from replay. */
    set(text) {
      buffer = String(text || '');
      schedule();
    },
    setLive(next) {
      if (isLive === Boolean(next)) return;
      isLive = Boolean(next);
      schedule();
    },
    clear() {
      buffer = '';
      schedule();
    },
    text: () => buffer,
    length: () => buffer.length,
    destroy() {
      if (frame != null) cancelAnimationFrame(frame);
      frame = null;
    },
  };
}

// ---------------------------------------------------------------------------
// parsing
// ---------------------------------------------------------------------------

/**
 * Split the accumulated text into ordered segments.
 *
 * Tolerant by design: an UNCLOSED `<thinking>` is the normal state while a
 * stage is streaming, and it renders as an open thinking block rather than as
 * literal angle brackets.
 *
 * @returns {Array<{kind: 'think'|'say'|'command', text: string}>}
 */
export function parseReasoning(raw) {
  const text = String(raw || '');
  const segments = [];
  let index = 0;

  const OPEN = '<thinking>';
  const CLOSE = '</thinking>';
  const CMD_OPEN = '<execute_command>';
  const CMD_CLOSE = '</execute_command>';

  while (index < text.length) {
    const think = text.indexOf(OPEN, index);
    const cmd = text.indexOf(CMD_OPEN, index);

    // Whichever tag comes first, if any.
    let next = -1;
    let kind = null;
    if (think >= 0 && (cmd < 0 || think < cmd)) {
      next = think;
      kind = 'think';
    } else if (cmd >= 0) {
      next = cmd;
      kind = 'command';
    }

    if (next < 0) {
      push(segments, 'say', text.slice(index));
      break;
    }

    push(segments, 'say', text.slice(index, next));

    if (kind === 'think') {
      const end = text.indexOf(CLOSE, next);
      if (end < 0) {
        push(segments, 'think', text.slice(next + OPEN.length));
        break;
      }
      push(segments, 'think', text.slice(next + OPEN.length, end));
      index = end + CLOSE.length;
    } else {
      const end = text.indexOf(CMD_CLOSE, next);
      const inner = end < 0 ? text.slice(next + CMD_OPEN.length) : text.slice(next + CMD_OPEN.length, end);
      push(segments, 'command', stripCommandTags(inner));
      index = end < 0 ? text.length : end + CMD_CLOSE.length;
    }
  }

  return segments;
}

function push(segments, kind, text) {
  const value = kind === 'say' ? text.replace(/^\n+/, '') : text;
  if (!value.trim()) return;
  const last = segments[segments.length - 1];
  // Consecutive say blocks are one paragraph run, not two, so the prose does
  // not gain a gap every time a thinking block closes mid-sentence.
  if (last && last.kind === kind && kind === 'say') {
    last.text += value;
    return;
  }
  segments.push({ kind, text: value });
}

function stripCommandTags(inner) {
  return inner
    .replace(/<\/?command>/g, '')
    .replace(/<\/?requires_approval>[\s\S]*?$/g, '')
    .trim();
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

function renderSegment(segment, isTail) {
  if (segment.kind === 'think') {
    return h(
      'div',
      { class: 'think' },
      h('p', { class: 'think__label' }, 'Thinking'),
      h('div', { class: ['think__text', isTail && 'cursor'] }, inline(segment.text.trim()))
    );
  }

  if (segment.kind === 'command') {
    return h(
      'div',
      { class: 'payload' },
      h('p', { class: 'payload__label' }, 'Command'),
      h('pre', { class: 'code-block code-block--wrap' }, segment.text)
    );
  }

  return h('div', { class: ['say', isTail && 'cursor'] }, inline(segment.text.trim()));
}

/**
 * `**strong**` and `` `code` `` only, as DOM nodes.
 *
 * A full markdown renderer would be wrong here twice over: the text is
 * untrusted, and the model's own emphasis is the only structure worth keeping —
 * headings and lists inside a thinking block are conversational, not document
 * structure.
 */
export function inline(text) {
  const nodes = [];
  const pattern = /\*\*([^*]+)\*\*|`([^`]+)`/g;
  let last = 0;
  let match;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
    if (match[1] !== undefined) nodes.push(h('b', null, match[1]));
    else nodes.push(h('code', null, match[2]));
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

/**
 * Pull the assistant text out of one `bob.message` event.
 * Returns '' for anything that is not assistant output, so the caller can fold
 * the whole event stream without a type switch.
 */
export function assistantText(event) {
  if (!event || event.type !== 'bob.message') return '';
  const data = event.data && typeof event.data === 'object' ? event.data : {};
  if (data.role && data.role !== 'assistant') return '';
  if (typeof data.text === 'string') return data.text;
  const payload = data.payload && typeof data.payload === 'object' ? data.payload : null;
  if (payload && payload.role === 'assistant' && typeof payload.content === 'string') {
    return payload.content;
  }
  return '';
}
