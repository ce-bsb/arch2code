/**
 * Theme pin.
 *
 * The CSS contract is one attribute on <html>: `data-theme="light" | "dark"`,
 * absent means "follow the operating system". Nothing else — every rule in
 * app.css reads through var(--cds-*), and `color-scheme` is switched alongside
 * so native scrollbars and form controls follow with no flash.
 *
 * The pin is written before first paint by an inline script in index.html; this
 * module only owns the toggle button and the persistence.
 */

const KEY = 'arch2code.theme';
const CYCLE = ['system', 'light', 'dark'];

const GLYPH = { system: '◐', light: '☀', dark: '☾' };
const LABEL = {
  system: 'Theme: follow the system',
  light: 'Theme: light (g10)',
  dark: 'Theme: dark (g100)',
};

export function readTheme() {
  try {
    const stored = localStorage.getItem(KEY);
    return CYCLE.includes(stored) ? stored : 'system';
  } catch (err) {
    // Private browsing or a locked-down profile. Following the OS is the right
    // fallback, and losing the preference is not worth a visible error.
    return 'system';
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
  else delete root.dataset.theme;
  try {
    if (theme === 'system') localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme);
  } catch (err) {
    /* the attribute is already applied; persistence is a nicety */
  }
}

/** Wire a button to cycle system → light → dark. */
export function mountThemeToggle(buttonEl) {
  if (!buttonEl) return;
  let theme = readTheme();

  function paint() {
    buttonEl.textContent = GLYPH[theme];
    buttonEl.setAttribute('aria-label', LABEL[theme]);
    buttonEl.setAttribute('title', `${LABEL[theme]} — click to change`);
  }

  buttonEl.addEventListener('click', () => {
    theme = CYCLE[(CYCLE.indexOf(theme) + 1) % CYCLE.length];
    applyTheme(theme);
    paint();
  });

  applyTheme(theme);
  paint();
}
