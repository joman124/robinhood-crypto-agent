"use client";

import { useRouter } from "next/navigation";
import { createContext, useContext, useEffect, useState } from "react";

import { ago } from "@/lib/format";

const REFRESH_MS = 60_000;
const TICK_MS = 15_000;

const NowContext = createContext<{ now: number; mounted: boolean }>({ now: 0, mounted: false });

/**
 * One clock for the page, seeded with the server's render time so the first
 * client render matches the HTML exactly, then ticking. It also re-fetches the
 * server component every minute while the tab is visible, so a page left open
 * shows new syncs without a manual reload.
 */
export function LiveProvider({
  serverNow,
  children,
}: {
  serverNow: number;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [state, setState] = useState({ now: serverNow, mounted: false });

  useEffect(() => {
    setState({ now: Date.now(), mounted: true });
    const tick = setInterval(() => setState({ now: Date.now(), mounted: true }), TICK_MS);
    const refresh = setInterval(() => {
      if (document.visibilityState === "visible") router.refresh();
    }, REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") router.refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(tick);
      clearInterval(refresh);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [router]);

  return <NowContext.Provider value={state}>{children}</NowContext.Provider>;
}

export function useNow() {
  return useContext(NowContext);
}

/** "5m ago", with the absolute local time on hover once the client knows its zone. */
export function RelTime({ iso }: { iso: string | null | undefined }) {
  const { now, mounted } = useNow();
  if (!iso) return <span>—</span>;
  return (
    <time dateTime={iso} title={mounted ? new Date(iso).toLocaleString() : iso}>
      {ago(iso, now)}
    </time>
  );
}
