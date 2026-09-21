/**
 * The decisions the agent pulls back.
 *
 * Read-only over the bearer token. Decisions are *written* by a server action
 * behind the session cookie (see `app/actions.ts`), never by an HTTP endpoint —
 * so there is no route a stranger could POST a forged "accept" to.
 */

import { NextResponse } from "next/server";

import { authorize } from "@/lib/auth";
import { getDecisions } from "@/lib/store";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const auth = authorize(request);
  if (!auth.ok) {
    return NextResponse.json({ error: auth.error }, { status: auth.status });
  }
  return NextResponse.json({ decisions: await getDecisions() });
}
