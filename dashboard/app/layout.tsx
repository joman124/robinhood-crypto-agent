import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Robinhood crypto agent — proposals",
  description:
    "Trade proposals from the robinhood-crypto-agent, their measured accuracy, and accept/decline sign-off.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
