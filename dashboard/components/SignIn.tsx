"use client";

import { useState, useTransition } from "react";

import { signIn, signOut } from "@/app/actions";

export function SignInForm() {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
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
      <h1>Sign in</h1>
      <p>Accepting or declining a proposal needs the dashboard password.</p>
      <input type="password" name="password" placeholder="Password" autoFocus />
      <button type="submit" disabled={pending}>
        {pending ? "Checking…" : "Sign in"}
      </button>
      {error && <div className="error">{error}</div>}
    </form>
  );
}

export function SignOutButton() {
  const [pending, startTransition] = useTransition();
  return (
    <button className="link" disabled={pending} onClick={() => startTransition(() => signOut().then(() => undefined))}>
      sign out
    </button>
  );
}
