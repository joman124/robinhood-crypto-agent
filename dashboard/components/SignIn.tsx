"use client";

import { useState, useTransition } from "react";

import { signIn, signOut } from "@/app/actions";

export function SignInForm() {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <main className="login-wrap">
      <form
        className="login"
        action={(formData) => {
          setError(null);
          startTransition(async () => {
            const result = await signIn(formData);
            if (!result.ok) setError(result.error ?? "sign in failed");
          });
        }}
      >
        <svg viewBox="0 0 32 32" width="36" height="36" aria-hidden="true">
          <rect width="32" height="32" rx="8" className="brand-mark" />
          <path d="M7 21l6-6 4 4 8-9" className="brand-line" />
        </svg>
        <h1>Robinhood crypto agent</h1>
        <p>This dashboard shows trade proposals and records your decisions. Sign in to continue.</p>
        <label className="sr-only" htmlFor="password">
          Password
        </label>
        <input id="password" type="password" name="password" placeholder="Password" autoFocus required />
        <button type="submit" className="primary" disabled={pending}>
          {pending ? "Checking…" : "Sign in"}
        </button>
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
      </form>
    </main>
  );
}

export function SignOutButton() {
  const [pending, startTransition] = useTransition();
  return (
    <button
      className="link"
      disabled={pending}
      onClick={() => startTransition(() => signOut().then(() => undefined))}
    >
      Sign out
    </button>
  );
}
