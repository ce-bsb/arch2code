/**
 * EventSource wrapper for the run stream.
 *
 * The server replays the JSONL log from Last-Event-ID before it tails, so the
 * browser's automatic reconnect loses nothing — but a replay means we WILL see
 * ids we already have. Dedupe by monotonic id is therefore mandatory, not an
 * optimization: without it a reconnect would duplicate every timeline row.
 */

import { api } from './api.js';

/** The full server vocabulary. Shared with state.js so a drift stays visible. */
export const EVENT_TYPES = [
  'run.created',
  'run.started',
  'run.stage.started',
  'run.stage.finished',
  // Stage 2 failed and its AIR was rebuilt mechanically from extraction.json,
  // or could not be. See app/air_fallback.py.
  'run.stage.fallback',
  'run.stage.fallback_unavailable',
  // A stage failed before Bob produced anything, the failure was upstream
  // unavailability (a 5xx from the key validator, a dropped socket), and the
  // stage was started once more. See should_retry_stage() in app/pipeline.py.
  'run.stage.retry',
  'run.awaiting_input',
  'run.resumed',
  'run.finished',
  'run.failed',
  'run.blocked',
  'run.cancelled',
  'bob.init',
  'bob.message',
  'bob.tool_use',
  'bob.tool_result',
  'bob.error',
  'bob.result',
  'bob.unknown',
  'bob.stderr',
  'bob.empty_output',
  'proc.exit',
  'artifact.written',
  'artifact.missing',
  'gate.evaluated',
  'vision.capture.started',
  'vision.capture.finished',
  'vision.extract.started',
  'vision.extract.finished',
  'vision.verify.finished',
  'vision.tool_error',
  'script.finished',
  'log',
];

/** Events after which the server closes the stream; we must not treat that as a drop. */
const TERMINAL_TYPES = new Set(['run.finished', 'run.failed', 'run.blocked', 'run.cancelled']);

/**
 * Open a live stream for a run.
 *
 * @param {string} runId
 * @param {object} opts
 * @param {number} opts.after         highest id already held by the client
 * @param {(event: object) => void} opts.onEvent
 * @param {() => void} [opts.onOpen]
 * @param {(info: {reconnecting: boolean, detail: string}) => void} [opts.onError]
 * @param {(info: {terminal: boolean}) => void} [opts.onClose]
 * @returns {{close: () => void, lastId: () => number}}
 */
export function openRunStream(runId, { after = 0, onEvent, onOpen, onError, onClose } = {}) {
  let lastId = Number(after) || 0;
  let terminal = false;
  let closed = false;
  const seen = new Set();

  const source = new EventSource(api.streamUrl(runId, lastId));

  const handle = (messageEvent) => {
    if (closed) return;
    const parsed = parseFrame(messageEvent);
    if (!parsed) return;

    // Dedupe on replay. Ids are monotonic per run, so `<= lastId` is enough for
    // the common case; the Set covers an out-of-order delivery we did not expect.
    if (parsed.id > 0) {
      if (parsed.id <= lastId || seen.has(parsed.id)) return;
      seen.add(parsed.id);
      lastId = Math.max(lastId, parsed.id);
    }

    if (TERMINAL_TYPES.has(parsed.type)) terminal = true;
    try {
      if (onEvent) onEvent(parsed);
    } catch (err) {
      // A rendering bug in one row must never kill the stream.
      // eslint-disable-next-line no-console
      console.error('run stream handler failed', parsed.type, err);
    }
  };

  source.addEventListener('open', () => {
    if (onOpen) onOpen();
  });

  // EVENT_TYPES is NOT an optimization — it is the delivery list. app/sse.py
  // writes an `event: <type>` line on every frame, and a named SSE frame is
  // dispatched ONLY to a listener for that exact name; the 'message' listener
  // below catches unnamed frames and nothing else. So a type the server emits
  // and this array omits is silently invisible in the live UI (it reappears on
  // reload, because that path reads run.json instead). Adding a server event
  // means adding it here.
  for (const type of EVENT_TYPES) source.addEventListener(type, handle);
  source.addEventListener('message', handle);

  source.addEventListener('error', () => {
    if (closed) return;
    if (terminal || source.readyState === EventSource.CLOSED) {
      // Either the run ended and the server hung up, or the browser gave up.
      closed = true;
      source.close();
      if (onClose) onClose({ terminal });
      return;
    }
    if (onError) {
      onError({
        reconnecting: true,
        detail:
          'Lost the event stream. The browser is retrying and will resume from the last event id, so nothing is lost.',
      });
    }
  });

  return {
    close() {
      if (closed) return;
      closed = true;
      source.close();
      if (onClose) onClose({ terminal });
    },
    lastId: () => lastId,
  };
}

/**
 * Parse one SSE frame into the shared Event envelope.
 * Never throws: an unreadable frame degrades to a `bob.unknown`-style row that
 * still carries the raw payload, which is the whole point of the escape hatch.
 */
function parseFrame(messageEvent) {
  const rawId = Number(messageEvent.lastEventId);
  const id = Number.isFinite(rawId) ? rawId : 0;
  const rawData = messageEvent.data;
  if (rawData == null || rawData === '') return null;

  let payload = null;
  try {
    payload = JSON.parse(rawData);
  } catch (err) {
    return {
      id,
      ts: new Date().toISOString(),
      run_id: null,
      stage: null,
      type: 'bob.unknown',
      data: { raw: String(rawData), parse_error: String(err && err.message ? err.message : err) },
    };
  }

  if (!payload || typeof payload !== 'object') {
    return {
      id,
      ts: new Date().toISOString(),
      run_id: null,
      stage: null,
      type: 'bob.unknown',
      data: { raw: String(rawData) },
    };
  }

  return {
    id: Number.isFinite(Number(payload.id)) ? Number(payload.id) : id,
    ts: payload.ts || new Date().toISOString(),
    run_id: payload.run_id || null,
    stage: payload.stage || null,
    type: payload.type || messageEvent.type || 'bob.unknown',
    data: payload.data && typeof payload.data === 'object' ? payload.data : {},
  };
}
