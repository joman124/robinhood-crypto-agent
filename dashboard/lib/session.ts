/**
 * Who is allowed to accept or decline.
 *
 * The agent replays an accepted decision into its approval gate, so a public
 * URL with Accept buttons would let a stranger put a trade in front of the
 * agent. That is the thing this file prevents.
 *
 * The model is deliberately small: one shared password in `DASHBOARD_PASSWORD`,
 * exchanged for an httpOnly cookie holding an HMAC of a fixed marker under that
 * password. The cookie cannot be forged without the password and carries no
 * user data. It is not a user system and is not trying to be.
 *
 * **If `DASHBOARD_PASSWORD` is unset the dashboard is read-only** — it still
 * shows proposals and accuracy, but no decision can be recorded, and the page
 * says so. Unset must mean "no one can decide", never "anyone can".
 *
 * For anything stronger, put Vercel Deployment Protection in front of the whole
 * deployment; the two compose.
 */

import { createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";

export const COOKIE_NAME = "rhca_session";
const MARKER = "rhca-dashboard-v1";

export function passwordConfigured(): boolean {
  const password = process.env.DASHBOARD_PASSWORD;
  return Boolean(password && password.length > 0);
}

function expectedCookieValue(): string | null {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) return null;
  return createHmac("sha256", password).update(MARKER).digest("hex");
}

function equals(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  if (left.length !== right.length) return false;
  return timingSafeEqual(left, right);
}

export function checkPassword(candidate: string): string | null {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password || !candidate) return null;
  if (!equals(candidate, password)) return null;
  return expectedCookieValue();
}

/** Whether the current request may record decisions. */
export async function isAuthenticated(): Promise<boolean> {
  const expected = expectedCookieValue();
  if (!expected) return false;
  const jar = await cookies();
  const presented = jar.get(COOKIE_NAME)?.value;
  if (!presented) return false;
  return equals(presented, expected);
}

/** Decisions are possible only with a password set AND a valid session. */
export async function canDecide(): Promise<boolean> {
  return passwordConfigured() && (await isAuthenticated());
}
