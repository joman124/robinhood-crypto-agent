/**
 * Ingest from `rhca dashboard-sync`, and read back for the agent.
 *
 * Both directions require the bearer token. The dashboard page itself does not
 * use this route — it reads the store directly on the server — so there is no
 * public read path to leave open by accident.
 */

import { NextResponse } from "next/server";

import { authorize } from "@/lib/auth";
import { getPayload, setPayload, storageMode } from "@/lib/store";
import type { Payload } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Reject an oversized body before parsing it. */
const MAX_BODY_BYTES = 4 * 1024 * 1024;

export async function POST(request: Request) {
  const auth = authorize(request);
  if (!auth.ok) {
    return NextResponse.json({ error: auth.error }, { status: auth.status });
  }

  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) {
    return NextResponse.json(
      { error: `payload exceeds ${MAX_BODY_BYTES} bytes` },
      { status: 413 },
    );
  }

  let payload: Payload;
  try {
    payload = JSON.parse(raw) as Payload;
  } catch {
    return NextResponse.json({ error: "body is not valid JSON" }, { status: 400 });
  }

  if (!payload || !Array.isArray(payload.proposals)) {
    return NextResponse.json(
      { error: "payload must contain a proposals array" },
      { status: 400 },
    );
  }

  await setPayload(payload);
  return NextResponse.json({
    stored: payload.proposals.length,
    storage: storageMode(),
    generated_at: payload.generated_at ?? null,
  });
}

export async function GET(request: Request) {
  const auth = authorize(request);
  if (!auth.ok) {
    return NextResponse.json({ error: auth.error }, { status: auth.status });
  }
  return NextResponse.json((await getPayload()) ?? { proposals: [] });
}
