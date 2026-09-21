/**
 * Persistence, with an honest fallback.
 *
 * Vercel KV (or any Upstash-compatible Redis) is used when its env vars are
 * present. Without them the dashboard keeps state in process memory, which is
 * correct for `npm run dev` and **wrong in production** — serverless instances
 * do not share memory, so a decision recorded by one invocation is invisible to
 * the next.
 *
 * Rather than hide that, `storageMode()` reports which backend is live and the
 * UI says so. A dashboard that silently forgets your decisions is worse than
 * one that tells you it will.
 *
 * Talking to the REST API with plain `fetch` avoids a dependency, and the two
 * calls used here (GET/SET on one key) are stable across both providers.
 */

import type { Decision, Payload } from "./types";

const PAYLOAD_KEY = "rhca:payload";
const DECISIONS_KEY = "rhca:decisions";

/** Bound the decision log so one key cannot grow without limit. */
const MAX_DECISIONS = 2000;

/**
 * The in-memory fallback is pinned to `globalThis`, not a module-level `const`.
 *
 * Next.js bundles route handlers and server actions separately, so a
 * module-scoped Map is instantiated once *per bundle*: a payload written by
 * `POST /api/proposals` would be invisible to the server action that reads it,
 * even in a single local process. Hanging it off `globalThis` gives the whole
 * process one store. (This is the same reason a database client is a global
 * singleton in Next apps.)
 *
 * This makes local development work correctly. It does NOT make the fallback
 * viable in production -- separate serverless instances still have separate
 * globals, which is what `storageMode()` warns about.
 */
const globalStore = globalThis as typeof globalThis & {
  __rhcaMemory?: Map<string, string>;
};
const memory: Map<string, string> = (globalStore.__rhcaMemory ??= new Map());

function kvConfig(): { url: string; token: string } | null {
  const url = process.env.KV_REST_API_URL;
  const token = process.env.KV_REST_API_TOKEN;
  if (!url || !token) return null;
  return { url: url.replace(/\/$/, ""), token };
}

export type StorageMode = "kv" | "memory";

export function storageMode(): StorageMode {
  return kvConfig() ? "kv" : "memory";
}

async function readKey(key: string): Promise<string | null> {
  const config = kvConfig();
  if (!config) return memory.get(key) ?? null;

  const response = await fetch(`${config.url}/get/${encodeURIComponent(key)}`, {
    headers: { Authorization: `Bearer ${config.token}` },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`KV read failed: HTTP ${response.status}`);
  }
  const body = (await response.json()) as { result?: string | null };
  return body.result ?? null;
}

async function writeKey(key: string, value: string): Promise<void> {
  const config = kvConfig();
  if (!config) {
    memory.set(key, value);
    return;
  }

  const response = await fetch(`${config.url}/set/${encodeURIComponent(key)}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${config.token}`,
      "Content-Type": "application/json",
    },
    body: value,
  });
  if (!response.ok) {
    throw new Error(`KV write failed: HTTP ${response.status}`);
  }
}

function parse<T>(raw: string | null): T | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export async function getPayload(): Promise<Payload | null> {
  return parse<Payload>(await readKey(PAYLOAD_KEY));
}

export async function setPayload(payload: Payload): Promise<void> {
  await writeKey(PAYLOAD_KEY, JSON.stringify(payload));
}

export async function getDecisions(): Promise<Decision[]> {
  return parse<Decision[]>(await readKey(DECISIONS_KEY)) ?? [];
}

/**
 * Append a decision, superseding any earlier one for the same proposal.
 *
 * A mind can be changed: accepting and then declining must leave exactly one
 * live decision, and it must be the later one.
 */
export async function recordDecision(decision: Decision): Promise<Decision[]> {
  const existing = await getDecisions();
  const others = existing.filter((d) => d.proposal_id !== decision.proposal_id);
  const next = [...others, decision]
    .sort((a, b) => a.decided_at.localeCompare(b.decided_at))
    .slice(-MAX_DECISIONS);
  await writeKey(DECISIONS_KEY, JSON.stringify(next));
  return next;
}

export async function clearDecision(proposalId: string): Promise<Decision[]> {
  const next = (await getDecisions()).filter((d) => d.proposal_id !== proposalId);
  await writeKey(DECISIONS_KEY, JSON.stringify(next));
  return next;
}
