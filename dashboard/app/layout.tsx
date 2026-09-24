import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Robinhood crypto agent",
  description:
    "Trade proposals from the robinhood-crypto-agent, their measured accuracy, and accept/decline sign-off.",
  // A private operator console: keep it out of search indexes.
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
