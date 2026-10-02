import { LiveProvider } from "@/components/Live";
import { AgentState, Nav, StatusAlerts, TopBar } from "@/components/ShellClient";
import { SignOutButton } from "@/components/SignIn";
import type { Console } from "@/lib/console";

/** The frame every console page sits in: sidebar, top bar, the real-money line, alerts. */
export function Shell({ c, children }: { c: Console; children: React.ReactNode }) {
  const awaiting = c.rows.filter((r) => r.fate === "awaiting").length;

  return (
    <LiveProvider serverNow={Date.now()}>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true"><i /><i /></span>
            <span>
              <strong>The split</strong>
            </span>
          </div>
          <Nav awaiting={awaiting} />
          <AgentState payload={c.payload} />
          <div className="operator">
            <span className="avatar" aria-hidden="true">OP</span>
            <span>
              <strong>{c.authed ? "Operator" : "Read only"}</strong>
            </span>
            {c.authed && <SignOutButton />}
          </div>
        </aside>

        <div className="workspace">
          <TopBar payload={c.payload} readOnly={!c.passwordSet} memory={c.storage === "memory"} />

          <main className="content">
            <StatusAlerts payload={c.payload} />
            {children}
          </main>
        </div>
      </div>
    </LiveProvider>
  );
}
