import type { Metadata } from "next";
import { DM_Sans, Inter, Roboto_Mono } from "next/font/google";

import "./globals.css";
import "./motion.css";

/** DM Sans for headings, Inter for reading, Roboto Mono for every number and id. */
const display = DM_Sans({ subsets: ["latin"], variable: "--font-display", display: "swap" });
const body = Inter({ subsets: ["latin"], variable: "--font-body", display: "swap" });
const mono = Roboto_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });

export const metadata: Metadata = {
  title: "The split · operator console",
  description:
    "Trade proposals from the robinhood-crypto-agent, their risk verdicts, and accept/decline sign-off by proposal ID.",
  // A private operator console: keep it out of search indexes.
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${body.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
