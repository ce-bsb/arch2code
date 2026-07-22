/**
 * Bounding-box overlay drawn over the normalized capture.
 *
 * Implementation note (deliberate deviation from the canvas sketch in the module
 * contract): the boxes are absolutely-positioned <button> elements sized in
 * PERCENT inside a position:relative container. Percent units mean the browser
 * re-lays out the boxes on every resize, zoom and devicePixelRatio change with no
 * ResizeObserver and no redraw loop, and — the reason that actually matters —
 * each box is a real focusable element, so the whole overlay is reachable by
 * keyboard and announced by a screen reader. A canvas would have needed a
 * parallel hit-test path plus a hidden DOM mirror to get the same result.
 *
 * The coordinates are only valid against the image the model actually saw, which
 * is the capture_diagram.py output (EXIF-corrected, longest edge <= 1568). The
 * caller must pass the normalized variant, never the original upload.
 */

import { confidenceBucket, confidenceLabel, h, isValidBbox } from '../util.js';

export { isValidBbox };

/** Denormalize a 0..1 bbox against a pixel box. Exported for tests and callers. */
export function bboxToPixels(bbox, width, height) {
  if (!isValidBbox(bbox)) return null;
  const [x, y, w, hh] = bbox.map(Number);
  return { x: x * width, y: y * height, w: w * width, h: hh * height };
}

/**
 * @param {HTMLElement} layerEl  the absolutely-positioned layer over the image
 * @param {HTMLImageElement} imgEl
 * @param {object} handlers  {onSelect(id), onHover(id|null)}
 */
export function createOverlay(layerEl, imgEl, { onSelect, onHover } = {}) {
  let items = [];
  let selected = null;
  let hovered = null;
  let alerts = new Set();
  const nodes = new Map();

  function render() {
    layerEl.textContent = '';
    nodes.clear();

    for (const item of items) {
      if (!isValidBbox(item.bbox)) continue;
      const [x, y, w, hh] = item.bbox.map(Number);
      const bucket = confidenceBucket(item.confidence);
      const isAlert = alerts.has(item.id);

      const box = h(
        'button',
        {
          type: 'button',
          class: [
            'ov-box',
            `ov-${item.kind === 'connection' ? 'connection' : 'component'}`,
            `ov-conf-${bucket}`,
            isAlert && 'ov-alert',
          ],
          style: {
            left: `${x * 100}%`,
            top: `${y * 100}%`,
            width: `${Math.max(w, 0.004) * 100}%`,
            height: `${Math.max(hh, 0.004) * 100}%`,
          },
          dataset: { id: item.id, kind: item.kind },
          'aria-pressed': String(selected === item.id),
          title: tooltipText(item, isAlert),
          'aria-label': ariaLabel(item, isAlert),
          onclick: (event) => {
            event.preventDefault();
            const next = selected === item.id ? null : item.id;
            setSelected(next);
            if (onSelect) onSelect(next);
          },
          onmouseenter: () => {
            setHovered(item.id);
            if (onHover) onHover(item.id);
          },
          onmouseleave: () => {
            setHovered(null);
            if (onHover) onHover(null);
          },
          onfocus: () => {
            setHovered(item.id);
            if (onHover) onHover(item.id);
          },
          onblur: () => {
            setHovered(null);
            if (onHover) onHover(null);
          },
        },
        h('span', { class: 'ov-tag' }, `${item.label} · ${confidenceLabel(item.confidence)}`)
      );

      nodes.set(item.id, box);
      layerEl.appendChild(box);
    }
    applyClasses();
  }

  function applyClasses() {
    for (const [id, node] of nodes) {
      node.classList.toggle('is-selected', id === selected);
      node.classList.toggle('is-hovered', id === hovered);
      node.setAttribute('aria-pressed', String(id === selected));
      // Dim everything else once something is selected, so a dense drawing
      // stops competing with the item under discussion.
      node.classList.toggle('is-dimmed', Boolean(selected) && id !== selected);
    }
    layerEl.classList.toggle('has-selection', Boolean(selected));
  }

  function setSelected(id) {
    // No scrollIntoView here: the boxes live inside the image frame, and
    // scrolling the page every time a list row is clicked would fight the user.
    selected = id || null;
    applyClasses();
  }

  function setHovered(id) {
    hovered = id || null;
    applyClasses();
  }

  return {
    /**
     * @param {Array} nextItems [{id, kind, label, confidence, bbox}]
     * @param {object} opts {alerts: string[]} ids stroked in the alert colour
     *   whatever their confidence — a broken reference is not a confidence problem.
     */
    setElements(nextItems, opts = {}) {
      items = Array.isArray(nextItems) ? nextItems.filter((item) => item && isValidBbox(item.bbox)) : [];
      alerts = new Set(Array.isArray(opts.alerts) ? opts.alerts : []);
      render();
    },
    setSelected,
    setHovered,
    /** Number of items that carried a usable bbox — the caller reports the gap. */
    drawnCount: () => items.length,
    redraw: render,
    focus(id) {
      const node = nodes.get(id);
      if (node) node.focus();
    },
    destroy() {
      layerEl.textContent = '';
      nodes.clear();
      items = [];
    },
  };
}

function tooltipText(item, isAlert) {
  const parts = [`${item.label}`, `kind: ${item.kindLabel || item.kind}`, `confidence: ${confidenceLabel(item.confidence)}`];
  if (isAlert) parts.push('flagged: broken reference');
  return parts.join('\n');
}

function ariaLabel(item, isAlert) {
  return `${item.kindLabel || item.kind} ${item.label}, confidence ${confidenceLabel(item.confidence)}${
    isAlert ? ', flagged as a broken reference' : ''
  }`;
}
