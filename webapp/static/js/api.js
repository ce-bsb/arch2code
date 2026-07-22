/**
 * Thin fetch wrapper over the local FastAPI backend.
 *
 * Every failure path funnels into ApiError, which mirrors the server's ErrorBody
 * {code, title, detail, remedy}. The UI renders `remedy` as the primary text, so
 * a request that fails without one is a defect: we synthesize a remedy here for
 * the transport-level cases (server down, body not JSON) rather than letting a
 * panel go blank.
 */

export class ApiError extends Error {
  constructor({ code, title, detail, remedy, status, context }) {
    super(title || detail || code || 'Request failed');
    this.name = 'ApiError';
    this.code = code || 'unknown_error';
    this.title = title || 'Request failed';
    this.detail = detail || '';
    this.remedy = remedy || null;
    this.status = status ?? 0;
    this.context = context || {};
  }
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/**
 * Issue a request and normalize the outcome.
 * `body` is JSON-encoded; `form` is passed through as FormData untouched.
 */
export async function request(method, path, { body, form, signal, headers } = {}) {
  const init = { method, signal, headers: { ...(headers || {}) } };
  if (form) {
    init.body = form;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    Object.assign(init.headers, JSON_HEADERS);
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError({
      code: 'network_unreachable',
      title: 'Cannot reach the local server',
      detail: `${method} ${path} failed before a response arrived: ${err && err.message ? err.message : err}`,
      remedy: 'Check that ./run.sh is still running in your terminal, then reload this page.',
      status: 0,
    });
  }

  if (response.status === 204) return null;

  const contentType = response.headers.get('content-type') || '';
  const isJson = contentType.includes('application/json');
  let payload = null;
  let rawText = '';
  if (isJson) {
    try {
      payload = await response.json();
    } catch (err) {
      payload = null;
    }
  } else {
    try {
      rawText = await response.text();
    } catch (err) {
      rawText = '';
    }
  }

  if (!response.ok) {
    throw toApiError(response, payload, rawText, method, path);
  }
  return isJson ? payload : rawText;
}

function toApiError(response, payload, rawText, method, path) {
  // The server's own ErrorBody, either bare or wrapped by FastAPI's `detail`.
  const body = pickErrorBody(payload);
  if (body) {
    return new ApiError({
      code: body.code,
      title: body.title,
      detail: body.detail,
      remedy: body.remedy,
      context: body.context,
      status: response.status,
    });
  }
  return new ApiError({
    code: `http_${response.status}`,
    title: `${response.status} ${response.statusText || 'Error'}`,
    detail: `${method} ${path} returned a body this client could not interpret. ${truncateRaw(rawText || safeJson(payload))}`,
    remedy:
      'Look at the terminal running ./run.sh for the traceback — an unhandled server exception is the usual cause.',
    status: response.status,
  });
}

function pickErrorBody(payload) {
  if (!payload || typeof payload !== 'object') return null;
  if (typeof payload.code === 'string' && typeof payload.title === 'string') return payload;
  const detail = payload.detail;
  if (detail && typeof detail === 'object' && typeof detail.code === 'string') return detail;
  return null;
}

function safeJson(value) {
  try {
    return JSON.stringify(value);
  } catch (err) {
    return '';
  }
}

function truncateRaw(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  return value.length > 300 ? `${value.slice(0, 299)}…` : value;
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : '';
}

/**
 * Upload with progress. fetch() cannot report upload progress, so this one call
 * uses XMLHttpRequest; every other call goes through `request`.
 */
export function uploadFile(file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/uploads');
    xhr.responseType = 'text';

    if (typeof onProgress === 'function' && xhr.upload) {
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      });
    }

    xhr.addEventListener('load', () => {
      let payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch (err) {
        payload = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        if (payload) resolve(payload);
        else
          reject(
            new ApiError({
              code: 'upload_bad_response',
              title: 'Upload succeeded but the response was unreadable',
              detail: truncateRaw(xhr.responseText),
              remedy: 'Reload the page and check GET /api/uploads to see whether the file was stored.',
              status: xhr.status,
            })
          );
        return;
      }
      const body = pickErrorBody(payload);
      reject(
        new ApiError({
          code: body ? body.code : `http_${xhr.status}`,
          title: body ? body.title : `Upload failed (${xhr.status})`,
          detail: body ? body.detail : truncateRaw(xhr.responseText),
          remedy: body
            ? body.remedy
            : xhr.status === 413
              ? 'The file is larger than ARCH2CODE_MAX_UPLOAD_MB. Downscale the image or raise the limit in webapp/.env.'
              : 'Check the terminal running ./run.sh for the server-side reason.',
          context: body ? body.context : {},
          status: xhr.status,
        })
      );
    });

    xhr.addEventListener('error', () =>
      reject(
        new ApiError({
          code: 'network_unreachable',
          title: 'Cannot reach the local server',
          detail: 'The upload request failed at the transport level.',
          remedy: 'Check that ./run.sh is still running, then try again.',
          status: 0,
        })
      )
    );
    xhr.addEventListener('abort', () =>
      reject(
        new ApiError({
          code: 'upload_aborted',
          title: 'Upload cancelled',
          detail: 'The upload was aborted before it finished.',
          remedy: 'Pick the file again to retry.',
          status: 0,
        })
      )
    );

    xhr.send(form);
  });
}

export const api = {
  // -- health ---------------------------------------------------------------
  health: () => request('GET', '/api/health'),
  recheckHealth: () => request('POST', '/api/health/recheck'),

  // -- uploads --------------------------------------------------------------
  uploadFile,
  listUploads: (limit = 50) => request('GET', `/api/uploads${query({ limit })}`),
  uploadFileUrl: (uploadId) => `/api/uploads/${encodeURIComponent(uploadId)}/file`,

  // -- runs -----------------------------------------------------------------
  createRun: (body) => request('POST', '/api/runs', { body }),
  startRun: (runId) => request('POST', `/api/runs/${encodeURIComponent(runId)}/start`),
  getRun: (runId) => request('GET', `/api/runs/${encodeURIComponent(runId)}`),
  listRuns: (params = {}) => request('GET', `/api/runs${query(params)}`),
  cancelRun: (runId) => request('POST', `/api/runs/${encodeURIComponent(runId)}/cancel`),
  deleteRun: (runId) => request('DELETE', `/api/runs/${encodeURIComponent(runId)}`),
  decideGate: (runId, decision) =>
    request('POST', `/api/runs/${encodeURIComponent(runId)}/gate`, { body: decision }),
  getStage: (runId, stageId, tail = 200) =>
    request(
      'GET',
      `/api/runs/${encodeURIComponent(runId)}/stages/${encodeURIComponent(stageId)}${query({ tail })}`
    ),

  // -- artifacts ------------------------------------------------------------
  listArtifacts: (runId) => request('GET', `/api/runs/${encodeURIComponent(runId)}/artifacts`),
  artifactUrl: (runId, artifactId, download = false) =>
    `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}${query({
      download: download ? 'true' : '',
    })}`,
  fetchArtifactText: async (runId, artifactId) => {
    const response = await fetch(api.artifactUrl(runId, artifactId));
    if (!response.ok) {
      let payload = null;
      try {
        payload = await response.json();
      } catch (err) {
        payload = null;
      }
      const body = pickErrorBody(payload);
      throw new ApiError({
        code: body ? body.code : `http_${response.status}`,
        title: body ? body.title : `Artifact unavailable (${response.status})`,
        detail: body ? body.detail : 'The artifact could not be read.',
        remedy: body
          ? body.remedy
          : 'Re-open the artifact list — a stage may have exited 0 without writing the file it contracted to write.',
        status: response.status,
      });
    }
    return {
      text: await response.text(),
      mediaType: response.headers.get('content-type') || 'text/plain',
    };
  },
  runImageUrl: (runId, variant = 'normalized') =>
    `/api/runs/${encodeURIComponent(runId)}/image${query({ variant })}`,

  // -- export ---------------------------------------------------------------
  /**
   * The bundle of everything a run produced. Owned by another module; this
   * client only calls it.
   */
  exportUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/export`,
  exportCodeUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/export/code`,
  /**
   * Every file the run wrote ANYWHERE under the project root, at its real path.
   * Distinct from exportCodeUrl, which carries only the contracted build
   * directory: the scaffold also writes real project trees outside it and only
   * describes them in its manifest.
   */
  exportProjectUrl: (runId) => `/api/runs/${encodeURIComponent(runId)}/export/project`,

  /**
   * What a download would contain, before spending the bytes.
   *
   * This is also how the UI decides whether to OFFER a download at all, and it
   * replaces the HEAD probe that used to live here. That probe was wrong: this
   * server answers HEAD /export with 404 while GET /export streams a valid ZIP,
   * so the most prominent button in the delivery screen never rendered. The
   * preview endpoint runs the same planner as the download, so it can never
   * disagree with it — including about what is missing.
   *
   * Never throws. A refusal comes back as {available: false} carrying the
   * server's own explanation, which is exactly what the card renders.
   */
  exportPreview: async (runId, kind = 'full') => {
    const url = `/api/runs/${encodeURIComponent(runId)}/export/preview${query({ kind })}`;
    try {
      const response = await fetch(url);
      let payload = null;
      try {
        payload = await response.json();
      } catch (err) {
        payload = null;
      }
      if (!response.ok) {
        const body = pickErrorBody(payload) || {};
        return {
          available: false,
          title: body.title || `The ${kind} download is not available`,
          detail: body.detail || '',
          remedy: body.remedy || '',
        };
      }
      return { available: true, ...(payload || {}) };
    } catch (err) {
      return {
        available: false,
        title: 'Cannot reach the local server',
        detail: `GET ${url} failed before a response arrived.`,
        remedy: 'Check that ./run.sh is still running, then reload this page.',
      };
    }
  },

  // -- targets (optional; the codegen profiles) ------------------------------
  /**
   * Platform targets, when this build ships them. Returns [] when the endpoint
   * does not exist yet, so the picker simply does not appear rather than the
   * upload screen failing to render.
   */
  listTargets: async () => {
    try {
      const response = await fetch('/api/targets');
      if (!response.ok) return [];
      const payload = await response.json();
      if (Array.isArray(payload)) return payload;
      if (payload && Array.isArray(payload.targets)) return payload.targets;
      return [];
    } catch (err) {
      return [];
    }
  },

  // -- vision ---------------------------------------------------------------
  getVision: (runId) => request('GET', `/api/runs/${encodeURIComponent(runId)}/vision`),
  verifyElement: (runId, body) =>
    request('POST', `/api/runs/${encodeURIComponent(runId)}/vision/verify`, { body }),
  listVerifications: (runId, targetId) =>
    request(
      'GET',
      `/api/runs/${encodeURIComponent(runId)}/vision/verifications${query({ target_id: targetId })}`
    ),

  // -- events ---------------------------------------------------------------
  replayEvents: (runId, after = 0, limit = 500, types) =>
    request('GET', `/api/runs/${encodeURIComponent(runId)}/events${query({ after, limit, types })}`),
  streamUrl: (runId, after = 0) =>
    `/api/runs/${encodeURIComponent(runId)}/stream${query({ after: after || '' })}`,
};

export default api;
