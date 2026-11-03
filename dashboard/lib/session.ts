/**
 * Who is allowed to accept or decline.
 *
 * The agent replays an accepted decision into its approval gate, so a public
 * URL with Accept buttons would let a stranger put a trade in front of the
 * agent. That is the thing this file prevents.
 *
 * The model is deliberately small: one shared password in `DASHBOARD_PASSWORD`,
 * exchanged for an httpOnly cookie holding the time it was issued and an HMAC
 * of that time under the password. The cookie cannot be forged or re-dated
 * without the password, carries no user data, and the server refuses it after
 * `SESSION_MS` -- so a copied cookie stops working on its own, not only when
 * the browser drops it. It is not a user system and is not trying to be.
 * ponytail: no revocation list; signing out clears this browser only. Rotate
 * `DASHBOARD_PASSWORD` to end every session at once.
 *
 * **If `DASHBOARD_PASSWORD` is unset the dashboard is read-only** — it still
 * shows proposals and accuracy, but no decision can be recorded, and the page
 * says so. Unset must mean "no one can decide", never "anyone can".
 *
 * For anything stronger, put Vercel Deployment Protection in front of the whole
 * deployment; the two compose.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

export const COOKIE_NAME = "rhca_session";
const MARKER = "rhca-dashboard-v2";
/** How long a sign-in lasts, enforced here and mirrored in the cookie's maxAge. */
export const SESSION_MS = 12 * 60 * 60 * 1000;

export function passwordConfigured(): boolean {
  const password = process.env.DASHBOARD_PASSWORD;
  return Boolean(password && password.length > 0);
}

function mac(password: string, issued: number): string {
  return createHmac("sha256", password).update(`${MARKER}.${issued}`).digest("hex");
}

/** The cookie value for a session issued at `issued` (ms since epoch). */
export function sessionValue(password: string, issued: number): string {
  return `${issued}.${mac(password, issued)}`;
}

/** Signed with this password, issued in the past, and younger than SESSION_MS. */
export function sessionValid(password: string, value: string, now: number): boolean {
  if (!password || !value) return false;
  const [stamp, presented] = value.split(".");
  const issued = Number(stamp);
  if (!Number.isSafeInteger(issued) || !presented) return false;
  if (issued > now || now - issued >= SESSION_MS) return false;
  return equals(presented, mac(password, issued));
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
  return sessionValue(password, Date.now());
}

/** Whether the current request may record decisions. */
export async function isAuthenticated(): Promise<boolean> {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) return false;
  // Loaded here so lib/session.check.mjs can run the pure helpers under plain node.
  const { cookies } = await import("next/headers");
  const jar = await cookies();
  return sessionValid(password, jar.get(COOKIE_NAME)?.value ?? "", Date.now());
}

/** Decisions are possible only with a password set AND a valid session. */
export async function canDecide(): Promise<boolean> {
  return passwordConfigured() && (await isAuthenticated());
}
