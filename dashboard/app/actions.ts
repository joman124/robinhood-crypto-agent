"use server";

/**
 * Server actions: the only way a decision is written.
 *
 * Every action re-checks the session on the server. A disabled button in the
 * UI is a courtesy, not a control — the check that matters is here.
 */

import { revalidatePath } from "next/cache";
import { cookies } from "next/headers";

import { clearDecision, getPayload, recordDecision } from "@/lib/store";
import { COOKIE_NAME, canDecide, checkPassword, passwordConfigured } from "@/lib/session";
import type { DecisionKind } from "@/lib/types";

/** Proposal ids are hex digests; anything else never reaches the store. */
const PROPOSAL_ID = /^[0-9a-f]{6,64}$/;
const MAX_NOTE = 500;

/**
 * The agent redacts this phrase too. Doing it on both sides means two
 * independent failures would be needed for a web note to carry an override.
 */
const OVERRIDE_PATTERN = /override\s+risk\s+check/gi;

function cleanNote(note: unknown): string {
  if (typeof note !== "string") return "";
  return note
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .replace(OVERRIDE_PATTERN, "[redacted]")
    .trim()
    .slice(0, MAX_NOTE);
}

export type ActionResult = { ok: boolean; error?: string };

export async function decide(
  proposalId: string,
  kind: DecisionKind,
  note?: string,
): Promise<ActionResult> {
  if (!(await canDecide())) {
    return {
      ok: false,
      error: passwordConfigured()
        ? "Sign in to record a decision."
        : "DASHBOARD_PASSWORD is not set, so this dashboard is read-only.",
    };
  }

  if (!PROPOSAL_ID.test(proposalId)) {
    return { ok: false, error: "that is not a valid proposal id" };
  }
  if (kind !== "accept" && kind !== "decline") {
    return { ok: false, error: "a decision must be accept or decline" };
  }

  // A risk-blocked proposal cannot be accepted from here. The agent's gate
  // refuses it too; refusing at both ends means the UI never offers something
  // the agent would reject.
  const payload = await getPayload();
  const proposal = payload?.proposals.find((p) => p.proposal_id === proposalId);
  if (!proposal) {
    return { ok: false, error: "no such proposal — re-sync the dashboard" };
  }
  if (kind === "accept" && !proposal.actionable) {
    return {
      ok: false,
      error:
        "the risk engine blocked this proposal, so it cannot be accepted here. " +
        "Overriding a risk block is a deliberate act from the terminal.",
    };
  }

  await recordDecision({
    proposal_id: proposalId,
    decision: kind,
    decided_at: new Date().toISOString(),
    source: "dashboard",
    actor: "owner",
    note: cleanNote(note),
  });

  revalidatePath("/");
  return { ok: true };
}

export async function undecide(proposalId: string): Promise<ActionResult> {
  if (!(await canDecide())) {
    return { ok: false, error: "not signed in" };
  }
  if (!PROPOSAL_ID.test(proposalId)) {
    return { ok: false, error: "that is not a valid proposal id" };
  }
  await clearDecision(proposalId);
  revalidatePath("/");
  return { ok: true };
}

export async function signIn(formData: FormData): Promise<ActionResult> {
  const candidate = String(formData.get("password") ?? "");
  const value = checkPassword(candidate);
  if (!value) {
    return { ok: false, error: "incorrect password" };
  }
  const jar = await cookies();
  jar.set(COOKIE_NAME, value, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 60 * 60 * 12,
  });
  revalidatePath("/");
  return { ok: true };
}

export async function signOut(): Promise<ActionResult> {
  const jar = await cookies();
  jar.delete(COOKIE_NAME);
  revalidatePath("/");
  return { ok: true };
}
