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
        <span className="brand-mark big" aria-hidden="true"><i /><i /></span>
        <h1>The split</h1>
        <label className="sr-only" htmlFor="password">
          Password
        </label>
        <input id="password" type="password" name="password" placeholder="Password" autoFocus required />
        <button type="submit" className="btn primary wide" disabled={pending}>
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
