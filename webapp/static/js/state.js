/**
 * Client store and the pure reducer over the run event vocabulary.
 *
 * `reduceEvent` returns a NEW state object and never mutates its input, so a
 * view can hold on to a previous state and diff against it. Every branch is
 * defensive: an event type nobody here recognizes still lands on the timeline as
 * a generic entry, which is exactly the contract bob.unknown was designed for.
 */

import { asArray, isPlainObject } from './util.js';

/** Maximum timeline rows retained in memory. A long scaffold stage is chatty. */
const MAX_TIMELINE = 2000;

export function createStore(initial) {
  let state = initial;
  const listeners = new Set();

  return {
    getState: () => state,
    setState(next) {
      const value = typeof next === 'function' ? next(state) : next;
      if (value === state) return state;
      state = value;
      for (const listener of listeners) {
        try {
          listener(state);
        } catch (err) {
          // eslint-disable-next-line no-console
          console.error('store subscriber failed', err);
        }
      }
      return state;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

export function initialRunState() {
  return {
    run: null,
    timeline: [],
    stages: [],
    gate: null,
    artifacts: [],
    totals: { tokens_in: 0, tokens_out: 0, duration_ms: 0, coins: null },
    // Cost is separate from totals because it is reported per Bob SESSION (one
    // session per stage) on the `result` line, and because the fields live in
    // the raw payload rather than in the StageStats the server extracts.
    // Every field starts null: "nothing was reported" and "zero was reported"
    // are different facts and the UI must be able to tell them apart.
    cost: { sessionCosts: null, budgetSpend: null, maxBudget: null, toolCalls: null },
    // Usage is keyed BY STAGE and the totals are derived from this map, never
    // added up as events arrive. The same figures reach the client twice — once
    // on `bob.result` and again on `run.stage.finished` — and a naive `+=`
    // doubles every token count in the app. Keying by stage makes a repeated
    // report idempotent, which is also what makes a mid-run reload safe.
    usageByStage: {},
    lastEventId: 0,
    streaming: false,
    reconnecting: false,
    terminal: false,
    stderrByStage: {},
    emptyStdoutStages: [],
    missingArtifacts: [],
    error: null,
  };
}

/** Hydrate from GET /api/runs/{id}; keeps whatever the timeline already holds. */
export function hydrateRun(state, run) {
  if (!run) return state;
  const stages = asArray(run.stages);
  // Seed the per-stage usage map from run.json so a run opened cold shows its
  // cost immediately, and so replaying the log afterwards overwrites rather
  // than adds.
  const usageByStage = { ...state.usageByStage };
  for (const stage of stages) {
    if (stage && stage.id && isPlainObject(stage.stats)) {
      usageByStage[stage.id] = { ...(usageByStage[stage.id] || {}), stats: stage.stats };
    }
  }
  return {
    ...state,
    run,
    stages,
    gate: run.gate || null,
    usageByStage,
    totals: deriveTotals(usageByStage),
    cost: deriveCost(usageByStage),
    lastEventId: Math.max(state.lastEventId, Number(run.last_event_id) || 0),
    terminal: isTerminalStatus(run.status),
    error: run.error || null,
  };
}

export function isTerminalStatus(status) {
  return status === 'succeeded' || status === 'failed' || status === 'blocked' || status === 'cancelled';
}

/**
 * Fold one event into the state. Pure. Never throws.
 */
export function reduceEvent(state, event) {
  if (!event || typeof event !== 'object') return state;
  const data = isPlainObject(event.data) ? event.data : {};
  const next = {
    ...state,
    lastEventId: Math.max(state.lastEventId, Number(event.id) || 0),
    timeline: appendTimeline(state.timeline, event),
  };

  switch (event.type) {
    case 'run.created':
      next.stages = asArray(data.stages).map((spec, index) => ({
        id: spec.id,
        index: spec.index ?? index + 1,
        title: spec.title || spec.id,
        slug: spec.slug || null,
        approval_mode: spec.approval_mode || null,
        status: 'pending',
        artifacts: [],
      }));
      next.run = next.run ? { ...next.run, status: 'created' } : next.run;
      break;

    case 'run.started':
      next.run = withStatus(next.run, 'running');
      next.terminal = false;
      break;

    case 'run.stage.started':
      next.stages = patchStage(next.stages, data.stage, (stage) => ({
        ...stage,
        status: 'running',
        started_at: event.ts,
        argv: asArray(data.argv),
        cwd: data.cwd || null,
        strategy: data.strategy || null,
        approval_mode: data.approval_mode ?? stage.approval_mode,
        timeout_s: data.timeout_s ?? null,
        error: null,
      }));
      next.run = withStatus(next.run, 'running');
      break;

    case 'run.stage.finished': {
      const artifacts = asArray(data.artifacts);
      next.stages = patchStage(next.stages, data.stage, (stage) => ({
        ...stage,
        status: data.status || 'succeeded',
        finished_at: event.ts,
        duration_ms: data.duration_ms ?? stage.duration_ms ?? null,
        exit_code: data.exit_code ?? null,
        stdout_lines: data.stdout_lines ?? stage.stdout_lines ?? 0,
        empty_stdout: Boolean(data.empty_stdout),
        used_pty: Boolean(data.used_pty),
        stats: data.stats || stage.stats || null,
        artifacts,
        error: data.error || null,
      }));
      next.artifacts = mergeArtifacts(next.artifacts, artifacts);
      next.usageByStage = recordUsage(next.usageByStage, data.stage || event.stage, { stats: data.stats });
      next.totals = deriveTotals(next.usageByStage);
      next.cost = deriveCost(next.usageByStage);
      break;
    }

    case 'bob.result': {
      const stage = event.stage || data.stage || 'unknown';
      const raw = isPlainObject(data.payload) && isPlainObject(data.payload.stats) ? data.payload.stats : {};
      next.usageByStage = recordUsage(next.usageByStage, stage, { stats: data.stats, raw, seq: event.id });
      next.totals = deriveTotals(next.usageByStage);
      next.cost = deriveCost(next.usageByStage);
      break;
    }

    case 'bob.stderr': {
      const stage = event.stage || data.stage || 'unknown';
      const previous = next.stderrByStage[stage] || '';
      next.stderrByStage = {
        ...next.stderrByStage,
        [stage]: clipTail(previous + String(data.chunk || ''), 16000),
      };
      break;
    }

    case 'bob.empty_output': {
      const stage = event.stage || data.stage;
      next.emptyStdoutStages = next.emptyStdoutStages.includes(stage)
        ? next.emptyStdoutStages
        : [...next.emptyStdoutStages, stage];
      break;
    }

    case 'artifact.written':
      if (isPlainObject(data.artifact)) {
        next.artifacts = mergeArtifacts(next.artifacts, [data.artifact]);
      }
      break;

    case 'artifact.missing':
      next.missingArtifacts = [
        ...next.missingArtifacts,
        {
          stage: event.stage || data.stage || null,
          expected_path: data.expected_path || null,
          remedy: data.remedy || null,
        },
      ];
      break;

    case 'gate.evaluated':
      next.gate = {
        ...(next.gate || {}),
        verdict: data.verdict || 'absent',
        gate_line: data.gate_line ?? null,
        matched: data.matched ?? null,
        verdict_artifact_id: data.artifact_id ?? (next.gate ? next.gate.verdict_artifact_id : null),
        decided: false,
      };
      break;

    case 'run.awaiting_input': {
      const gate = isPlainObject(data.gate) ? data.gate : {};
      next.gate = {
        ...(next.gate || {}),
        verdict: gate.verdict || 'absent',
        gate_line: gate.gate_line ?? null,
        verdict_artifact_id: gate.verdict_artifact_id ?? null,
        verdict_excerpt: gate.verdict_excerpt || '',
        findings_count: gate.findings_count ?? null,
        choices: asArray(gate.choices).length ? asArray(gate.choices) : ['approve', 'block', 'send_back'],
        default_choice: gate.default_choice || null,
        decided: false,
      };
      next.run = withStatus(next.run, 'awaiting_input');
      next.stages = patchStage(next.stages, data.stage || 'critic', (stage) => ({
        ...stage,
        status: stage.status === 'running' ? 'succeeded' : stage.status,
      }));
      break;
    }

    case 'run.resumed':
      next.gate = {
        ...(next.gate || {}),
        decided: true,
        decision: data.decision || null,
        override: Boolean(data.override),
        reason: data.reason || null,
        resume_from: data.resume_from || null,
        decided_at: data.decided_at || event.ts,
      };
      next.run = withStatus(next.run, 'running');
      break;

    case 'run.finished':
      next.run = withStatus(next.run, 'succeeded');
      next.totals = data.totals ? { ...next.totals, ...data.totals } : next.totals;
      next.artifacts = mergeArtifacts(next.artifacts, asArray(data.artifacts));
      next.terminal = true;
      break;

    case 'run.failed':
      next.run = withStatus(next.run, 'failed');
      next.error = isPlainObject(data.error) ? data.error : next.error;
      if (data.stage) {
        next.stages = patchStage(next.stages, data.stage, (stage) => ({
          ...stage,
          status: stage.status === 'running' ? 'failed' : stage.status,
          error: isPlainObject(data.error) ? data.error : stage.error,
        }));
      }
      next.terminal = true;
      break;

    case 'run.blocked':
      next.run = withStatus(next.run, 'blocked');
      next.gate = { ...(next.gate || {}), decided: true, decision: 'block', reason: data.reason || null };
      next.terminal = true;
      break;

    case 'run.cancelled':
      next.run = withStatus(next.run, 'cancelled');
      next.stages = next.stages.map((stage) =>
        stage.status === 'running' ? { ...stage, status: 'failed' } : stage
      );
      next.terminal = true;
      break;

    case 'vision.extract.finished':
      next.extractSummary = {
        components: data.components ?? 0,
        connections: data.connections ?? 0,
        boundaries: data.boundaries ?? 0,
        unknowns: data.unknowns ?? 0,
        overall_confidence: data.overall_confidence ?? null,
        quality: isPlainObject(data.quality) ? data.quality : {},
        duration_ms: data.duration_ms ?? null,
      };
      break;

    // Stage 2 failed and its AIR was rebuilt mechanically from the intake's
    // extraction.json (see app/air_fallback.py). The stage stays FAILED on
    // purpose — it is degraded, not recovered — but it now owns an artifact and
    // a different error, and the run keeps going.
    case 'run.stage.fallback': {
      const artifacts = asArray(data.artifacts);
      next.stages = patchStage(next.stages, data.stage || event.stage, (stage) => ({
        ...stage,
        fallback: true,
        artifacts: artifacts.length ? artifacts : stage.artifacts,
        error: isPlainObject(data.error) ? data.error : stage.error,
      }));
      next.artifacts = mergeArtifacts(next.artifacts, artifacts);
      break;
    }

    // The stage failed upstream before producing anything and is being started
    // one more time. It stays `running`: nothing finished. The attempt count is
    // recorded so the stage carries a visible "retried" mark instead of the
    // second attempt looking like the only one there ever was.
    case 'run.stage.retry':
      next.stages = patchStage(next.stages, data.stage || event.stage, (stage) => ({
        ...stage,
        attempts: Number(data.next_attempt) || (Number(stage.attempts) || 1) + 1,
        retry_reason: data.reason || null,
      }));
      break;

    case 'vision.tool_error':
      next.error = {
        code: 'vision_tool_error',
        title: `${data.tool || 'The vision tool'} returned an error`,
        detail: data.message || 'No message was returned.',
        remedy: data.remedy || null,
      };
      break;

    default:
      // Unknown types are already on the timeline. Nothing else to do, and
      // deliberately no throw: the vocabulary is allowed to grow.
      break;
  }

  return next;
}

function appendTimeline(timeline, event) {
  const next = timeline.length >= MAX_TIMELINE ? timeline.slice(timeline.length - MAX_TIMELINE + 1) : timeline.slice();
  next.push(event);
  return next;
}

function withStatus(run, status) {
  if (!run) return run;
  return run.status === status ? run : { ...run, status };
}

function patchStage(stages, stageId, patch) {
  if (!stageId) return stages;
  let found = false;
  const next = stages.map((stage) => {
    if (stage.id !== stageId) return stage;
    found = true;
    return patch(stage);
  });
  // A stage the client never heard of (server-side plan drift) still shows up.
  if (!found) next.push(patch({ id: stageId, index: next.length + 1, title: stageId, status: 'pending' }));
  return next;
}

function mergeArtifacts(existing, incoming) {
  if (!incoming.length) return existing;
  const byId = new Map();
  for (const artifact of existing) {
    if (isPlainObject(artifact) && artifact.artifact_id) byId.set(artifact.artifact_id, artifact);
  }
  for (const artifact of incoming) {
    if (isPlainObject(artifact) && artifact.artifact_id) byId.set(artifact.artifact_id, artifact);
  }
  return Array.from(byId.values());
}

/**
 * Record what one stage reported. Idempotent by construction.
 *
 * `bob.result` carries the session stats; `run.stage.finished` carries the same
 * StageStats a second time. Both land on the same key, so the later one simply
 * replaces the earlier and nothing is counted twice — including on a reload,
 * where the whole log is replayed from event 0.
 */
function recordUsage(usage, stage, entry) {
  const key = stage || 'unknown';
  const previous = usage[key] || {};
  const merged = { ...previous };
  if (isPlainObject(entry.stats)) merged.stats = entry.stats;
  if (isPlainObject(entry.raw) && Object.keys(entry.raw).length) merged.raw = entry.raw;
  if (entry.seq != null) merged.seq = entry.seq;
  return { ...usage, [key]: merged };
}

/** Token and duration totals, summed over the stages that reported them. */
function deriveTotals(usage) {
  let tokensIn = 0;
  let tokensOut = 0;
  let duration = 0;
  for (const entry of Object.values(usage)) {
    const stats = isPlainObject(entry.stats) ? entry.stats : {};
    tokensIn += finite(stats.input_tokens);
    tokensOut += finite(stats.output_tokens);
    duration += finite(stats.duration_ms);
  }
  return { tokens_in: tokensIn, tokens_out: tokensOut, duration_ms: duration, coins: null };
}

/**
 * Cost, aggregated the way each figure actually behaves.
 *
 * `session_costs` is the cost of ONE session and every stage is a new session,
 * so it sums. `budget_spend` and `max_budget` are an account-level running
 * total and its ceiling — in the frozen sample, one 0.09-cost session reported
 * `budget_spend: 78.12` against `max_budget: 100`. Summing those would have
 * produced "234.36 / 100", which is how a cost meter loses a reviewer's trust
 * in one glance. The LATEST report wins instead.
 */
function deriveCost(usage) {
  let sessionCosts = null;
  let toolCalls = null;
  let budgetSpend = null;
  let maxBudget = null;
  let latestSeq = -1;

  for (const entry of Object.values(usage)) {
    const stats = isPlainObject(entry.stats) ? entry.stats : {};
    const raw = isPlainObject(entry.raw) ? entry.raw : {};

    const cost = Number(stats.session_costs ?? raw.session_costs);
    if (Number.isFinite(cost)) sessionCosts = (sessionCosts || 0) + cost;

    const calls = Number(raw.tool_calls);
    if (Number.isFinite(calls)) toolCalls = (toolCalls || 0) + calls;

    const seq = Number(entry.seq);
    const spend = Number(raw.budget_spend);
    if (Number.isFinite(spend) && (!Number.isFinite(seq) || seq >= latestSeq)) {
      budgetSpend = spend;
      const ceiling = Number(raw.max_budget);
      if (Number.isFinite(ceiling)) maxBudget = ceiling;
      if (Number.isFinite(seq)) latestSeq = seq;
    }
  }

  return { sessionCosts, budgetSpend, maxBudget, toolCalls };
}

function finite(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function clipTail(text, limit) {
  return text.length <= limit ? text : text.slice(text.length - limit);
}
