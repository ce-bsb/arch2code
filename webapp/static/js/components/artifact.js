/**
 * Artifacts: the file tree and the viewer.
 *
 * The highlighter is written by hand on purpose. No highlight.js, no Monaco, no
 * CDN — the app has to come up offline with one command, and colouring five
 * token classes is not worth a build step. It is deliberately MINIMAL: strings,
 * numbers, comments, keywords and punctuation. It never tries to parse; a
 * highlighter that mis-parses a file silently corrupts what the reviewer reads,
 * whereas one that only recognises lexemes degrades to plain text.
 *
 * Every colour comes from the six classes app.css already defines for JSON
 * (.j-key .j-str .j-num .j-bool .j-null .j-punct) plus .y-comment. No new CSS.
 *
 * Everything is built as DOM nodes, never as an HTML string: artifact content is
 * model output derived from a user-supplied image and is treated as hostile.
 */

import { api } from '../api.js';
import {
  copyText,
  formatBytes,
  formatDateTime,
  h,
  replaceChildren,
  safeStringify,
} from '../util.js';

const MAX_RENDER_BYTES = 2 * 1024 * 1024;
/** Above this, line numbers cost more than they are worth on a slow laptop. */
const MAX_GUTTER_LINES = 8000;

// ---------------------------------------------------------------------------
// viewer
// ---------------------------------------------------------------------------

export function createArtifactViewer(rootEl, { runIdRef } = {}) {
  let current = null;

  function showEmpty() {
    current = null;
    replaceChildren(
      rootEl,
      h('div', { class: 'empty-state' },
        h('p', { class: 'empty' }, 'No artifact selected.'),
        h('p', { class: 'empty-hint' },
          'Pick a file from the tree. An artifact listed as missing is itself the answer: ' +
          'a stage claimed success and wrote nothing.'))
    );
  }

  function showMissing(artifact) {
    replaceChildren(
      rootEl,
      h('div', { class: 'notice notice-error' },
        h('div', { class: 'notice__body' },
          h('p', { class: 'notice-title' }, 'This artifact does not exist on disk'),
          h('pre', { class: 'code-block code-block--wrap' }, artifact.path || artifact.rel_path || 'unknown path'),
          h('p', null,
            'The stage exited without writing what it contracted to write. The usual cause is ' +
            'arch-scaffold running under an approval mode that excludes write_to_file.'),
          h('p', { class: 'notice-remedy' },
            'Re-run the scaffold stage with --yolo. The app applies that policy automatically; ' +
            'if it did not, the stage’s tool calls show the exact argv it ran with.')))
    );
  }

  async function open(artifact) {
    if (!artifact) return showEmpty();
    current = artifact;
    const runId = typeof runIdRef === 'function' ? runIdRef() : runIdRef;

    if (artifact.exists === false) return showMissing(artifact);

    replaceChildren(rootEl, header(artifact, runId), h('p', { class: 'loading' }, 'Reading…'));

    if (Number(artifact.bytes) > MAX_RENDER_BYTES) {
      replaceChildren(
        rootEl,
        header(artifact, runId),
        h('div', { class: 'notice notice-warn' },
          h('div', { class: 'notice__body' },
            h('p', { class: 'notice-title' }, `${formatBytes(artifact.bytes)} is too large to render inline`),
            h('p', { class: 'notice-remedy' }, 'Use Download to open it in your editor.')))
      );
      return;
    }

    if (isImage(artifact.media_type)) {
      replaceChildren(
        rootEl,
        header(artifact, runId),
        h('img', {
          class: 'artifact-image',
          src: api.artifactUrl(runId, artifact.artifact_id),
          alt: artifact.rel_path || 'artifact image',
        })
      );
      return;
    }

    try {
      const { text } = await api.fetchArtifactText(runId, artifact.artifact_id);
      replaceChildren(rootEl, header(artifact, runId, text), renderContent(text, artifact));
    } catch (err) {
      replaceChildren(
        rootEl,
        header(artifact, runId),
        h('div', { class: 'notice notice-error' },
          h('div', { class: 'notice__body' },
            h('p', { class: 'notice-title' }, err.title || 'Could not read the artifact'),
            h('p', null, err.detail || ''),
            err.remedy ? h('p', { class: 'notice-remedy' }, err.remedy) : null))
      );
    }
  }

  showEmpty();
  return { open, showEmpty, current: () => current };
}

function header(artifact, runId, text) {
  return h(
    'div',
    { class: 'viewer__head' },
    h('div', null,
      h('p', { class: 'viewer__path' }, artifact.rel_path || artifact.path || 'artifact'),
      h('p', { class: 'viewer__meta' },
        [artifact.kind, formatBytes(artifact.bytes), artifact.mtime ? formatDateTime(artifact.mtime) : null]
          .filter(Boolean).join(' · '))),
    h('div', { class: 'btn-set push' },
      text
        ? h('button', {
            type: 'button',
            class: 'btn btn-sm btn-quiet',
            onclick: async (event) => {
              const ok = await copyText(text);
              const btn = event.currentTarget;
              btn.textContent = ok ? 'Copied' : 'Copy failed';
              setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
            },
          }, 'Copy')
        : null,
      h('a', {
        class: 'btn btn-sm btn-tertiary',
        href: api.artifactUrl(runId, artifact.artifact_id, true),
        download: '',
      }, 'Download this file'))
  );
}

// ---------------------------------------------------------------------------
// content dispatch
// ---------------------------------------------------------------------------

function renderContent(text, artifact) {
  const path = String(artifact.rel_path || artifact.path || '').toLowerCase();
  const mediaType = String(artifact.media_type || '').toLowerCase();

  if (path.endsWith('.json') || mediaType.includes('json')) {
    try {
      return withGutter(renderJson(JSON.parse(text)));
    } catch (err) {
      return h('div', null,
        h('div', { class: 'notice notice-warn' },
          h('div', { class: 'notice__body' },
            h('p', { class: 'notice-title' }, 'This file is named .json but does not parse'),
            h('p', null, String(err && err.message ? err.message : err)),
            h('p', { class: 'notice-remedy' },
              'The raw text is below exactly as written — that is usually enough to see the truncation.'))),
        withGutter(h('pre', { class: 'artifact-pre' }, text)));
    }
  }
  if (path.endsWith('.yaml') || path.endsWith('.yml')) return withGutter(renderYaml(text));
  if (path.endsWith('.md') || path.endsWith('.markdown')) return withGutter(renderMarkdown(text));
  return withGutter(renderCode(text, languageOf(path)));
}

/** Extension → the lexeme table used to colour it. Unknown means plain text. */
function languageOf(path) {
  if (/\.(py|pyi)$/.test(path)) return 'python';
  if (/\.(js|mjs|cjs|ts|tsx|jsx)$/.test(path)) return 'js';
  if (/\.(sh|bash|zsh|env|ini|cfg|toml)$/.test(path)) return 'shell';
  if (/dockerfile$/.test(path)) return 'shell';
  if (/\.(sql)$/.test(path)) return 'sql';
  if (/\.(cbl|cob|cpy|jcl)$/.test(path)) return 'cobol';
  if (/\.(txt|log|md5|sha256)$/.test(path)) return null;
  return 'generic';
}

// ---------------------------------------------------------------------------
// line-number gutter
// ---------------------------------------------------------------------------

/**
 * Wrap a rendered `<pre>` in a gutter of line numbers.
 *
 * The numbers are real DOM, aligned by a `ch`-based width so they stay put at
 * any font size, and `user-select: none` so copying the code does not copy the
 * numbers. Colour and the hairline come from `.gutter` in app.css; the only
 * inline style is the width, which depends on the digit count of this file.
 */
function withGutter(pre) {
  const text = pre.textContent;
  const count = text.split('\n').length;
  if (count < 2 || count > MAX_GUTTER_LINES) return pre;

  const digits = String(count).length;
  const gutter = h('span', {
    class: 'gutter',
    'aria-hidden': 'true',
    style: { width: `${digits + 1}ch` },
  });
  const numbers = [];
  for (let i = 1; i <= count; i += 1) numbers.push(String(i));
  gutter.textContent = numbers.join('\n');

  // `white-space: pre` on the code column, not pre-wrap: a wrapped line would
  // slide out of alignment with its number and the gutter would start lying.
  // Horizontal scrolling is the honest trade, and .artifact-pre already scrolls.
  const code = h('span', { class: 'code-col' });
  while (pre.firstChild) code.appendChild(pre.firstChild);

  pre.append(gutter, code);
  return pre;
}

// ---------------------------------------------------------------------------
// JSON
// ---------------------------------------------------------------------------

/** Recursive pretty-printer producing coloured DOM nodes. */
export function renderJson(value) {
  const pre = h('pre', { class: 'artifact-pre json-view' });
  appendJsonValue(pre, value, 0);
  return pre;
}

function appendJsonValue(parent, value, depth) {
  const pad = '  '.repeat(depth);
  const padIn = '  '.repeat(depth + 1);

  if (value === null) return parent.appendChild(tok('j-null', 'null'));
  if (typeof value === 'boolean') return parent.appendChild(tok('j-bool', String(value)));
  if (typeof value === 'number') return parent.appendChild(tok('j-num', String(value)));
  if (typeof value === 'string') return parent.appendChild(tok('j-str', JSON.stringify(value)));

  if (Array.isArray(value)) {
    if (!value.length) return parent.appendChild(tok('j-punct', '[]'));
    parent.appendChild(tok('j-punct', '['));
    value.forEach((item, index) => {
      parent.appendChild(document.createTextNode(`\n${padIn}`));
      appendJsonValue(parent, item, depth + 1);
      if (index < value.length - 1) parent.appendChild(tok('j-punct', ','));
    });
    parent.appendChild(document.createTextNode(`\n${pad}`));
    return parent.appendChild(tok('j-punct', ']'));
  }

  if (typeof value === 'object') {
    const entries = Object.entries(value);
    if (!entries.length) return parent.appendChild(tok('j-punct', '{}'));
    parent.appendChild(tok('j-punct', '{'));
    entries.forEach(([key, item], index) => {
      parent.appendChild(document.createTextNode(`\n${padIn}`));
      parent.appendChild(tok('j-key', JSON.stringify(key)));
      parent.appendChild(tok('j-punct', ': '));
      appendJsonValue(parent, item, depth + 1);
      if (index < entries.length - 1) parent.appendChild(tok('j-punct', ','));
    });
    parent.appendChild(document.createTextNode(`\n${pad}`));
    return parent.appendChild(tok('j-punct', '}'));
  }

  return parent.appendChild(tok('j-str', safeStringify(value)));
}

function tok(className, text) {
  const span = document.createElement('span');
  span.className = className;
  span.textContent = text;
  return span;
}

// ---------------------------------------------------------------------------
// YAML
// ---------------------------------------------------------------------------

export function renderYaml(text) {
  const pre = h('pre', { class: 'artifact-pre yaml-view' });
  const lines = String(text).split('\n');
  lines.forEach((line, index) => {
    appendYamlLine(pre, line);
    if (index < lines.length - 1) pre.appendChild(document.createTextNode('\n'));
  });
  return pre;
}

function appendYamlLine(pre, line) {
  const comment = line.match(/^(\s*)#(.*)$/);
  if (comment) {
    pre.appendChild(document.createTextNode(comment[1]));
    pre.appendChild(tok('y-comment', `#${comment[2]}`));
    return;
  }
  const entry = line.match(/^(\s*)(-\s+)?([A-Za-z0-9_.$-]+)(\s*:)(\s*)(.*)$/);
  if (entry) {
    pre.appendChild(document.createTextNode(entry[1]));
    if (entry[2]) pre.appendChild(tok('y-dash', entry[2]));
    pre.appendChild(tok('j-key', entry[3]));
    pre.appendChild(tok('j-punct', entry[4]));
    pre.appendChild(document.createTextNode(entry[5]));
    if (entry[6]) pre.appendChild(tok(yamlScalarClass(entry[6]), entry[6]));
    return;
  }
  const item = line.match(/^(\s*)(-\s+)(.*)$/);
  if (item) {
    pre.appendChild(document.createTextNode(item[1]));
    pre.appendChild(tok('y-dash', item[2]));
    pre.appendChild(tok(yamlScalarClass(item[3]), item[3]));
    return;
  }
  pre.appendChild(document.createTextNode(line));
}

function yamlScalarClass(value) {
  const trimmed = value.trim();
  if (/^(true|false|null|~)$/i.test(trimmed)) return 'j-bool';
  if (/^-?\d+(\.\d+)?$/.test(trimmed)) return 'j-num';
  return 'j-str';
}

// ---------------------------------------------------------------------------
// Markdown
// ---------------------------------------------------------------------------

/**
 * Readable monospaced markdown. Not a renderer: the point is to read verdict.md
 * and validation.md exactly as they were written, with the gate line — the
 * single most important line in the whole run — impossible to miss.
 */
export function renderMarkdown(text) {
  const pre = h('pre', { class: 'artifact-pre md-view' });
  let inFence = false;
  const lines = String(text).split('\n');

  lines.forEach((line, index) => {
    const isFence = /^\s*```/.test(line);
    if (isFence) {
      pre.appendChild(tok('md-fence', line));
      inFence = !inFence;
    } else if (inFence) {
      pre.appendChild(tok('md-code', line));
    } else if (/^\s*VERDICT:\s*(APPROVED|BLOCKED)\s*$/i.test(line)) {
      pre.appendChild(tok(/APPROVED/i.test(line) ? 'md-verdict-ok' : 'md-verdict-block', line));
    } else if (/^\s{0,3}#{1,6}\s/.test(line)) {
      pre.appendChild(tok('md-heading', line));
    } else if (/^\s*>/.test(line)) {
      pre.appendChild(tok('md-quote', line));
    } else if (/^\s*([-*+]|\d+\.)\s/.test(line)) {
      pre.appendChild(tok('md-list', line));
    } else {
      pre.appendChild(document.createTextNode(line));
    }
    if (index < lines.length - 1) pre.appendChild(document.createTextNode('\n'));
  });

  return pre;
}

// ---------------------------------------------------------------------------
// Generic source code
// ---------------------------------------------------------------------------

const KEYWORDS = {
  python: /^(def|class|return|import|from|as|if|elif|else|for|while|try|except|finally|with|raise|yield|lambda|async|await|pass|break|continue|in|is|not|and|or|None|True|False|self)$/,
  js: /^(function|class|const|let|var|return|import|export|from|default|if|else|for|while|try|catch|finally|throw|new|await|async|of|in|typeof|instanceof|null|undefined|true|false|this)$/,
  shell: /^(if|then|else|fi|for|do|done|while|case|esac|function|export|local|return|source|set|FROM|RUN|COPY|CMD|ENV|WORKDIR|ENTRYPOINT|EXPOSE|USER|ARG|LABEL)$/,
  sql: /^(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|ORDER|BY|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|TABLE|INDEX|AS|AND|OR|NOT|NULL|PRIMARY|KEY|FOREIGN|REFERENCES)$/i,
  cobol: /^(IDENTIFICATION|ENVIRONMENT|DATA|PROCEDURE|DIVISION|SECTION|PROGRAM-ID|WORKING-STORAGE|LINKAGE|FILE-CONTROL|SELECT|ASSIGN|FD|PIC|VALUE|MOVE|PERFORM|UNTIL|IF|ELSE|END-IF|OPEN|CLOSE|READ|WRITE|GOBACK|STOP|RUN|EXEC|CICS|END-EXEC|COPY)$/i,
  generic: /^(function|class|def|return|import|export|if|else|for|while|true|false|null|nil|none)$/i,
};

const COMMENT_PREFIX = {
  python: '#',
  shell: '#',
  sql: '--',
  js: '//',
  generic: null,
  cobol: null,
};

/**
 * Lexeme-level colouring. One pass, one regex, no state machine beyond the
 * per-line comment check — the failure mode of a hand-rolled highlighter is a
 * wrong parse, and the only defence is to never attempt a parse.
 */
export function renderCode(text, language) {
  const pre = h('pre', { class: 'artifact-pre' });
  if (!language) {
    pre.textContent = String(text);
    return pre;
  }

  const keywords = KEYWORDS[language] || KEYWORDS.generic;
  const commentPrefix = COMMENT_PREFIX[language];
  const lines = String(text).split('\n');

  lines.forEach((line, index) => {
    appendCodeLine(pre, line, keywords, commentPrefix, language);
    if (index < lines.length - 1) pre.appendChild(document.createTextNode('\n'));
  });
  return pre;
}

const TOKEN_RE = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_-]*)|([{}()[\],;:.=+\-*/<>!&|]+)/g;

function appendCodeLine(pre, line, keywords, commentPrefix, language) {
  // COBOL: columns 1-6 are sequence numbers, column 7 is the indicator. A '*'
  // there comments the whole line, and that is not a prefix match anywhere else.
  if (language === 'cobol' && /^.{6}\*/.test(line)) {
    pre.appendChild(tok('y-comment', line));
    return;
  }
  if (commentPrefix) {
    const at = indexOfUnquoted(line, commentPrefix);
    if (at >= 0) {
      appendCodeLine(pre, line.slice(0, at), keywords, null, language);
      pre.appendChild(tok('y-comment', line.slice(at)));
      return;
    }
  }

  let last = 0;
  let match;
  TOKEN_RE.lastIndex = 0;
  while ((match = TOKEN_RE.exec(line)) !== null) {
    if (match.index > last) pre.appendChild(document.createTextNode(line.slice(last, match.index)));
    const [whole, str, num, word, punct] = match;
    if (str !== undefined) pre.appendChild(tok('j-str', whole));
    else if (num !== undefined) pre.appendChild(tok('j-num', whole));
    else if (word !== undefined) {
      if (keywords.test(word)) pre.appendChild(tok('j-key', whole));
      else pre.appendChild(document.createTextNode(whole));
    } else if (punct !== undefined) pre.appendChild(tok('j-punct', whole));
    last = match.index + whole.length;
  }
  if (last < line.length) pre.appendChild(document.createTextNode(line.slice(last)));
}

/** Index of `needle` outside any quoted run, or -1. */
function indexOfUnquoted(line, needle) {
  let quote = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) {
      if (ch === '\\') i += 1;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
    if (line.startsWith(needle, i)) return i;
  }
  return -1;
}

function isImage(mediaType) {
  return String(mediaType || '').startsWith('image/');
}

// ---------------------------------------------------------------------------
// file tree
// ---------------------------------------------------------------------------

/**
 * Group artifacts by directory into a real tree.
 *
 * A flat list of `.arch/build/<run>/src/api/main.py` paths is unreadable at ten
 * files and hopeless at a hundred; the tree is what makes "the code it
 * generated" something a reviewer can actually walk. Directories are `<details>`
 * so the whole thing is keyboard-navigable with no JS.
 */
export function renderArtifactTree(artifacts, { onSelect, selectedId } = {}) {
  if (!artifacts.length) {
    return h('p', { class: 'empty' },
      'No artifacts yet. They appear as each stage writes the file it contracted to write.');
  }

  const root = { dirs: new Map(), files: [] };
  for (const artifact of artifacts) {
    const rel = String(artifact.rel_path || artifact.path || artifact.artifact_id || '');
    const parts = rel.split('/').filter(Boolean);
    const name = parts.pop() || rel;
    let node = root;
    for (const part of parts) {
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
      node = node.dirs.get(part);
    }
    node.files.push({ artifact, name });
  }

  return h('div', { class: 'tree' }, renderNode(root, '', { onSelect, selectedId }, 0));
}

/**
 * Collapse a chain of directories that each hold exactly one directory and
 * nothing else into a single row.
 *
 * Without this, the real trees this app produces render as
 * `.arch/` → `air/` → `20260722-0528-modeb2/` → one file: four rows and three
 * disclosure triangles to reach a single artifact, with the file itself
 * defaulted shut because it sits below the auto-open depth. Every file explorer
 * worth using does this; here it is the difference between a tree you can read
 * and a tree you give up on.
 */
function collapseChain(name, node) {
  let label = name;
  let current = node;
  while (current.files.length === 0 && current.dirs.size === 1) {
    const [childName, child] = [...current.dirs.entries()][0];
    label += `/${childName}`;
    current = child;
  }
  return [label, current];
}

function renderNode(node, prefix, handlers, depth) {
  const children = [];

  for (const [rawName, rawChild] of [...node.dirs.entries()].sort(byName)) {
    const [name, child] = collapseChain(rawName, rawChild);
    const count = countFiles(child);
    children.push(
      h('li', null,
        h('details',
          // Shallow directories start open; deep trees would otherwise bury the
          // one file the reviewer came for.
          { open: depth < 2 },
          h('summary', null,
            h('span', null, `${name}/`),
            h('span', { class: 'tree__size' }, `${count} file${count === 1 ? '' : 's'}`)),
          h('div', { style: { 'padding-left': '0.75rem' } },
            renderNode(child, `${prefix}${name}/`, handlers, depth + 1))))
    );
  }

  for (const { artifact, name } of node.files.sort((a, b) => a.name.localeCompare(b.name))) {
    const selected = artifact.artifact_id === handlers.selectedId;
    children.push(
      h('li', null,
        h('button', {
          type: 'button',
          class: ['tree__file', artifact.exists === false && 'is-missing', selected && 'is-selected'],
          'aria-current': selected ? 'true' : null,
          onclick: () => handlers.onSelect && handlers.onSelect(artifact),
        },
          h('span', null, name),
          h('span', { class: 'tree__size' },
            artifact.exists === false ? 'missing' : formatBytes(artifact.bytes))))
    );
  }

  return h('ul', null, children);
}

function byName(a, b) {
  return a[0].localeCompare(b[0]);
}

function countFiles(node) {
  let total = node.files.length;
  for (const child of node.dirs.values()) total += countFiles(child);
  return total;
}

/** Flat list, kept for callers that do not want the tree (the vision screen). */
export function renderArtifactList(artifacts, { onSelect, selectedId } = {}) {
  return renderArtifactTree(artifacts, { onSelect, selectedId });
}
