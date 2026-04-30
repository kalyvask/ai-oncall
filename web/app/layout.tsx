import type { Metadata } from "next";
import "./globals.css";
import { TopNav } from "@/components/TopNav";

export const metadata: Metadata = {
  title: "ai-oncall",
  description: "Ranked, evidence-backed RCA in under 30 seconds.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/*
          Geist is the single-family default; Fraunces is the optional
          page-hero serif. No Inter, no Roboto — both are flagged as the
          "AI default" by ai-tells.md and the impeccable reflex-reject list.
        */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="font-sans">
        <TopNav />
        {/* Container is wide; text columns inside cap themselves at 65ch. */}
        <main className="mx-auto max-w-[1180px] px-6 pb-16 pt-10 sm:px-10">
          {children}
        </main>
      </body>
    </html>
  );
}
