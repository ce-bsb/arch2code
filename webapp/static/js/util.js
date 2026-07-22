/**
 * Formatting and DOM helpers.
 *
 * No dependencies, no build step. Everything here is pure except `h`, which
 * builds detached DOM nodes. We never assemble markup by string concatenation:
 * extraction payloads carry text read off a user-supplied image, so treating any
 * of it as HTML would be an injection vector on our own machine.
 */

/**
 * Minimal hyperscript. `attrs` understands class, dataset, style objects,
 * `on*` handlers and plain attributes. Children may be nodes, strings, arrays,
 * null or false (falsy children are dropped, so `cond && node` works).
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null || value === false) continue;
      if (key === 'class' || key === 'className') {
        el.className = Array.isArray(value) ? value.filter(Boolean).join(' ') : String(value);
      } else if (key === 'dataset') {
        for (const [dk, dv] of Object.entries(value)) {
          if (dv != null) el.dataset[dk] = String(dv);
        }
      } else if (key === 'style' && typeof value === 'object') {
        for (const [sk, sv] of Object.entries(value)) {
          if (sv != null) el.style.setProperty(sk, String(sv));
        }
      } else if (key === 'text') {
        el.textContent = String(value);
      } else if (key.startsWith('on') && typeof value === 'function') {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) {
        el.setAttribute(key, '');
      } else {
        el.setAttribute(key, String(value));
      }
    }
  }
  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  for (const child of children) {
    if (child == null || child === false || child === true || child === '') continue;
    if (Array.isArray(child)) appendChildren(el, child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

/** Replace every child of `el` with `children`. */
export function replaceChildren(el, ...children) {
  el.textContent = '';
  appendChildren(el, children);
  return el;
}

export function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n < 1000) return `${Math.round(n)} ms`;
  const seconds = n / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${String(rest).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, '0')}m`;
}

/**
 * Wall-clock time of an ISO timestamp, local zone, second precision.
 *
 * The zone stays local — a reviewer correlating the timeline with their own
 * terminal needs their own clock — but the FORMAT is pinned to en-US, because
 * the rest of the interface is in English and a timeline that says "21 de jul."
 * in an otherwise English screenshot reads as a bug.
 */
export function formatClock(ts) {
  const date = toDate(ts);
  if (!date) return '—';
  return date.toLocaleTimeString('en-US', { hour12: false });
}

export function formatDateTime(ts) {
  const date = toDate(ts);
  if (!date) return '—';
  const day = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  return `${day} ${date.toLocaleTimeString('en-US', { hour12: false })}`;
}

export function toDate(ts) {
  if (!ts) return null;
  const date = ts instanceof Date ? ts : new Date(ts);
  return Number.isNaN(date.getTime()) ? null : date;
}

/**
 * Grouped integer. Locale is pinned to en-US on purpose: these are engineering
 * figures read off a screen share by people in several countries, and a token
 * count that renders `37.154` on one machine and `37,154` on another is a
 * number two reviewers will read differently by a factor of a thousand.
 */
export function formatNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

/**
 * Confidence buckets. The thresholds are the MCP server's own: it flags every
 * connection below 0.85 as needing verification, so 0.85 is the line between
 * "reported" and "must be checked by a human".
 */
export function confidenceBucket(confidence) {
  const n = Number(confidence);
  if (!Number.isFinite(n)) return 'unknown';
  if (n >= 0.85) return 'high';
  if (n >= 0.6) return 'medium';
  return 'low';
}

export function confidenceLabel(confidence) {
  const n = Number(confidence);
  if (!Number.isFinite(n)) return 'no confidence reported';
  return `${Math.round(n * 100)}%`;
}

export function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

export function truncate(text, limit = 240) {
  const value = String(text == null ? '' : text);
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`;
}

/** Pretty JSON text, tolerant of cycles and of non-serializable values. */
export function safeStringify(value, indent = 2) {
  const seen = new WeakSet();
  try {
    return JSON.stringify(value, (key, val) => {
      if (typeof val === 'object' && val !== null) {
        if (seen.has(val)) return '[circular]';
        seen.add(val);
      }
      return val;
    }, indent);
  } catch (err) {
    return String(value);
  }
}

/** Human label for a stage id, used wherever the server did not send a title. */
export const STAGE_TITLES = {
  capture: 'Capture',
  extract: 'Extract',
  intake: 'Intake',
  analyst: 'Analyst',
  critic: 'Critic',
  scaffold: 'Scaffold',
  validator: 'Validator',
};

export function stageTitle(stageId) {
  if (!stageId) return '—';
  return STAGE_TITLES[stageId] || stageId;
}

/** Copy text to the clipboard, falling back to a hidden textarea offline. */
export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    /* fall through to the legacy path */
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch (err) {
    return false;
  }
}

/**
 * Read a bbox off an extraction element.
 *
 * The MCP server writes `evidence: {kind: "bbox", value: [x, y, w, h]}` while the
 * app contract names the field `evidence.bbox`. Both are accepted, plus a bare
 * `bbox` on the element, because a payload that draws no boxes is a worse failure
 * than a slightly permissive reader.
 */
export function readBbox(element) {
  if (!element || typeof element !== 'object') return null;
  const evidence = element.evidence && typeof element.evidence === 'object' ? element.evidence : {};
  const candidates = [evidence.bbox, evidence.value, element.bbox];
  for (const candidate of candidates) {
    if (isValidBbox(candidate)) return candidate.map(Number);
  }
  return null;
}

/** Four finite numbers, each within 0..1 — the normalization the model was told to use. */
export function isValidBbox(bbox) {
  return (
    Array.isArray(bbox) &&
    bbox.length === 4 &&
    bbox.every((n) => Number.isFinite(Number(n)) && Number(n) >= 0 && Number(n) <= 1)
  );
}

export function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Coerce anything to an array, so a malformed payload cannot break a `.map`. */
export function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value == null) return [];
  return [value];
}
