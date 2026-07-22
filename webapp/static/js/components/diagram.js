/**
 * The drawing, with the boxes the model drew on it.
 *
 * This panel answers the first question anyone asks of this product: *did it
 * actually read my diagram, or is it making things up?* The bounding boxes are
 * the evidence, so getting their geometry right matters more than anything
 * cosmetic on this screen.
 *
 * TWO SOURCES, ONE PANEL
 * ----------------------
 * A vision-preview run persists `vision/extraction.json` next to the run and the
 * server hands it back already normalized in `_bboxes` (0..1, clamped). A full
 * pipeline run does not: Bob writes `.arch/intake/<run>/extraction.json` inside
 * the repository, the app sees it as an ARTIFACT, and the coordinates in it are
 * whatever the model emitted — in the real runs on disk, pixels against the
 * source image.
 *
 * So the panel tries the server's normalized copy first and falls back to the
 * artifact, normalizing pixel coordinates against the image's natural size. The
 * heuristic is deliberately blunt (any value above 1.5 means the payload is in
 * pixels) because the alternative — trusting a mixed payload — puts boxes in
 * plausible but wrong places, which is worse than drawing none.
 */

import { api } from '../api.js';
import { createOverlay } from './overlay.js';
import {
  asArray,
  confidenceBucket,
  formatNumber,
  h,
  isPlainObject,
  isValidBbox,
  readBbox,
  replaceChildren,
} from '../util.js';

export function createDiagramPanel(rootEl, { onSelect } = {}) {
  let runId = null;
  let overlay = null;
  let items = [];
  let alerts = [];

  const imgEl = h('img', { class: 'vision-image', alt: '' });
  const layerEl = h('div', { class: 'overlay-layer' });
  const frameEl = h('div', { class: 'image-frame' }, imgEl, layerEl);
  const legendEl = h('div', { class: 'image-legend' });
  const summaryEl = h('div', { class: 'stack stack--tight' });
  const noticeEl = h('div', { hidden: true });

  replaceChildren(
    rootEl,
    h('section', { class: 'surface surface--accent', 'aria-label': 'The drawing that was read' },
      h('div', { class: 'surface__head' },
        h('h2', { class: 'section-title' }, 'The drawing'),
        h('span', { class: 'meta', id: 'diagram-count' }, '')),
      h('div', { class: 'surface__body surface__body--flush' }, frameEl),
      h('div', { class: 'surface__foot' }, legendEl)),
    noticeEl,
    summaryEl
  );

  const countEl = rootEl.querySelector('#diagram-count');

  overlay = createOverlay(layerEl, imgEl, {
    onSelect: (id) => onSelect && onSelect(id),
  });

  // The boxes are percentages, so a resize needs no redraw — but the image's
  // natural size is what turns a pixel payload into percentages, and that is
  // only known once the image has decoded.
  imgEl.addEventListener('load', () => draw());

  async function load(run) {
    runId = run && run.run_id;
    if (!runId) return;

    const filename = (run.upload && run.upload.filename) || 'the uploaded drawing';
    imgEl.alt = `The architecture drawing that was read: ${filename}`;

    const result = await loadExtraction(runId);

    // The normalized capture is what the model actually saw. It only exists for
    // runs that went through capture_diagram.py inside this app; the original
    // upload is the honest fallback and is the same picture at a different size.
    //
    // Which one to ask for is decided from the preview's `image.ready` flag
    // rather than by requesting the normalized copy and handling the failure:
    // a 404 in the network panel during a demo reads as a broken app even when
    // the fallback works perfectly.
    imgEl.src = api.runImageUrl(runId, result && result.imageReady ? 'normalized' : 'original');

    apply(result, run);
  }

  /** Try the server's normalized copy, then the artifact Bob wrote. */
  async function loadExtraction(id) {
    let imageReady = false;
    try {
      const preview = await api.getVision(id);
      imageReady = Boolean(preview && preview.image && preview.image.ready);
      const payload = preview && preview.extraction;
      if (payload && asArray(payload.components).length) {
        return { payload, quality: preview.quality || {}, source: 'vision', imageReady };
      }
    } catch (err) {
      /* fall through: a pipeline run has no vision/ directory, which is normal */
    }

    try {
      const response = await api.listArtifacts(id);
      const artifact = ((response && response.artifacts) || []).find(
        (item) => item && item.exists !== false && /extraction\.json$/.test(String(item.rel_path || ''))
      );
      if (!artifact) return { payload: null, imageReady };
      const { text } = await api.fetchArtifactText(id, artifact.artifact_id);
      const payload = JSON.parse(text);
      return { payload, quality: (payload && payload._quality) || {}, source: 'artifact', imageReady };
    } catch (err) {
      return { payload: null, imageReady };
    }
  }

  function apply(result, run) {
    if (!result || !result.payload) {
      items = [];
      alerts = [];
      countEl.textContent = '';
      replaceChildren(legendEl,
        h('span', { class: 'meta' }, 'No extraction has been written yet — the boxes appear when intake finishes.'));
      renderSummary(null, run);
      overlay.setElements([]);
      return;
    }

    const payload = result.payload;
    const boxes = isPlainObject(payload._bboxes) ? payload._bboxes : {};
    const components = asArray(payload.components);
    const connections = asArray(payload.connections);
    const unknowns = asArray(payload.unknowns);

    items = [
      ...components.map((element) => toItem(element, 'component', boxes)),
      ...connections.map((element) => toItem(element, 'connection', boxes)),
    ].filter(Boolean);

    alerts = asArray(result.quality && result.quality.broken_refs);

    countEl.textContent = `${components.length} components · ${connections.length} connections`;

    replaceChildren(
      legendEl,
      h('span', { class: 'lg lg-high' }, 'confident'),
      h('span', { class: 'lg lg-medium' }, 'unsure'),
      h('span', { class: 'lg lg-low' }, 'needs a human'),
      h('span', { class: 'lg lg-conn' }, 'connection')
    );

    renderSummary({ components, connections, unknowns, quality: result.quality, source: result.source }, run);
    draw();
  }

  /**
   * Turn one extraction element into an overlay item.
   * Normalized geometry wins; a pixel payload is converted at draw time, when
   * the image's natural size is known.
   */
  function toItem(element, kind, boxes) {
    if (!isPlainObject(element)) return null;
    const id = element.id || element.name || element.label;
    if (!id) return null;

    const normalized = isValidBbox(boxes[id]) ? boxes[id].map(Number) : readBbox(element);
    const rawValue = element.evidence && Array.isArray(element.evidence.value)
      ? element.evidence.value.map(Number)
      : null;

    return {
      id,
      kind,
      kindLabel: kind === 'connection' ? 'connection' : element.kind || element.type || 'component',
      label: element.name || element.label || (element.evidence && element.evidence.label_text) || id,
      confidence: element.confidence,
      bbox: normalized,
      rawBbox: rawValue && rawValue.length === 4 && rawValue.some((n) => n > 1.5) ? rawValue : null,
    };
  }

  /** Hand the overlay geometry it can trust, converting pixels if we must. */
  function draw() {
    if (!items.length) {
      overlay.setElements([]);
      return;
    }
    const width = imgEl.naturalWidth || 0;
    const height = imgEl.naturalHeight || 0;

    const drawable = items
      .map((item) => {
        if (isValidBbox(item.bbox)) return item;
        if (item.rawBbox && width > 0 && height > 0) {
          const [x, y, w, hh] = item.rawBbox;
          const bbox = [x / width, y / height, w / width, hh / height];
          if (isValidBbox(bbox)) return { ...item, bbox };
        }
        return null;
      })
      .filter(Boolean);

    overlay.setElements(drawable, { alerts });
  }

  function renderSummary(data, run) {
    const facts = [];
    if (run && run.upload) {
      facts.push(['Source', run.upload.filename]);
      facts.push(['Read as', run.source_kind || 'screenshot']);
    }
    if (data) {
      facts.push(['Components', formatNumber(data.components.length)]);
      facts.push(['Connections', formatNumber(data.connections.length)]);
      if (data.unknowns.length) facts.push(['Unknowns', formatNumber(data.unknowns.length)]);
    }

    const blocking = data ? data.unknowns.filter((item) => item && item.blocking) : [];
    const lowConfidence = data ? data.connections.filter((item) => Number(item.confidence) < 0.85) : [];

    replaceChildren(
      summaryEl,
      h('section', { class: 'surface', 'aria-label': 'What was read off the drawing' },
        h('div', { class: 'surface__head' }, h('h2', { class: 'section-title' }, 'What it read')),
        h('div', { class: 'surface__body' },
          h('dl', { class: 'factlist' },
            facts.map(([label, value]) =>
              h('div', { class: 'fact' }, h('dt', null, label), h('dd', null, String(value))))),
          blocking.length
            ? h('div', { class: 'notice notice-warn', style: { 'margin-top': 'var(--a2c-space-05)' } },
                h('div', { class: 'notice__body' },
                  h('p', { class: 'notice-title' },
                    `${blocking.length} unknown${blocking.length === 1 ? '' : 's'} would block code generation`),
                  h('ul', { class: 'notice-list' },
                    blocking.slice(0, 4).map((item) =>
                      h('li', null, item.description || item.id || 'unnamed unknown')))))
            : null,
          lowConfidence.length
            ? h('p', { class: 'meta', style: { 'margin-top': 'var(--a2c-space-04)' } },
                `${lowConfidence.length} connection${lowConfidence.length === 1 ? ' was' : 's were'} ` +
                'extracted below the 0.85 threshold. Those are the ones the critic looks at first.')
            : null,
          data && data.source === 'artifact'
            ? h('p', { class: 'meta', style: { 'margin-top': 'var(--a2c-space-04)' } },
                'Boxes come from the extraction.json this run wrote, mapped onto the source image.')
            : null))
    );
  }

  return {
    load,
    select: (id) => overlay.setSelected(id),
    hover: (id) => overlay.setHovered(id),
    clear() {
      items = [];
      alerts = [];
      imgEl.removeAttribute('src');
      overlay.setElements([]);
      countEl.textContent = '';
      replaceChildren(legendEl);
      replaceChildren(summaryEl);
      noticeEl.hidden = true;
    },
    /** Confidence bucket of one id, so a list elsewhere can match the box colour. */
    bucketOf(id) {
      const item = items.find((entry) => entry.id === id);
      return item ? confidenceBucket(item.confidence) : 'unknown';
    },
  };
}
