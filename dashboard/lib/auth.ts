/**
 * Bearer-token auth for the sync endpoints.
 *
 * The token is shared with `rhca dashboard-sync`. It guards the write path so a
 * stranger cannot inject fake proposals or record decisions on the owner's
 * behalf — a fabricated "accept" is the thing worth preventing here, since the
 * agent replays decisions into its approval gate.
 */

import { timingSafeEqual } from "node:crypto";

export const TOKEN_ENV = "RHCA_DASHBOARD_TOKEN";

export function configuredToken(): string | null {
  const token = process.env[TOKEN_ENV];
  return token && token.length > 0 ? token : null;
}

/** Constant-time compare, so a wrong token leaks nothing through timing. */
function equals(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}

export type AuthResult = { ok: true } | { ok: false; status: number; error: string };

export function authorize(request: Request): AuthResult {
  const expected = configuredToken();
  if (!expected) {
    // Refuse rather than allow: an unset token must not mean "open to all".
    return {
      ok: false,
      status: 503,
      error: `${TOKEN_ENV} is not set on the server, so writes are refused.`,
    };
  }

  const header = request.headers.get("authorization") ?? "";
  const presented = header.toLowerCase().startsWith("bearer ")
    ? header.slice(7).trim()
    : "";

  if (!presented || !equals(presented, expected)) {
    return { ok: false, status: 401, error: "invalid or missing bearer token" };
  }
  return { ok: true };
}
