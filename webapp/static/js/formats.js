/**
 * The accepted-format table, mirrored from webapp/app/routing.py.
 *
 * This exists so the file picker's `accept` attribute and the "what can I drop
 * here" copy are derived from ONE list instead of drifting apart. The server
 * remains the authority: `route_artifact()` decides, and a file this table
 * accepts but the server refuses still gets a 415 with the exact reason. What
 * this table must never do is HIDE a format the server supports — that is the
 * failure the user notices, because they cannot tell the difference between
 * "unsupported" and "the picker would not let me choose it".
 */

export const VISION_EXT = [
  '.png', '.jpg', '.jpeg', '.webp', '.heic', '.heif', '.bmp', '.tif', '.tiff',
];

export const DETERMINISTIC_EXT = [
  '.drawio', '.xml', '.puml', '.plantuml', '.mmd', '.mermaid', '.md', '.json', '.yaml', '.yml',
];

export const PDF_EXT = ['.pdf'];

/** Extensions the watsonx chat endpoint reads without conversion first. */
export const MODEL_READY_EXT = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];

export const ALL_EXT = [...VISION_EXT, ...PDF_EXT, ...DETERMINISTIC_EXT];

/** The `accept` attribute for the file input. */
export const ACCEPT = ALL_EXT.join(',');

/** Human groups for the empty state, in the order a user thinks about them. */
export const FORMAT_GROUPS = [
  {
    title: 'Photographs and screenshots',
    hint: 'Read by watsonx vision after normalization',
    exts: VISION_EXT,
  },
  {
    title: 'Structured sources',
    hint: 'Parsed exactly — no tokens spent, nothing invented',
    exts: DETERMINISTIC_EXT,
  },
  {
    title: 'PDF',
    hint: 'Text first; the vision path only if the page is a pure image',
    exts: PDF_EXT,
  },
];
