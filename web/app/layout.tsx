import type { Metadata } from "next";
import type { ReactNode } from "react";
import { SessionProvider } from "@/components/auth";
import { Shell } from "@/components/shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "AEGIS workbench",
  description: "Runs, reviews, tests and approvals for the AEGIS coding platform.",
};

// Applies a saved theme before first paint, so a dark-theme user never sees a light flash.
const THEME = `try{var t=localStorage.getItem("aegis.theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME }} />
      </head>
      <body className="min-h-screen font-sans antialiased">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-md focus:bg-accent focus:px-3 focus:py-2 focus:text-accent-text"
        >
          Skip to content
        </a>
        <SessionProvider>
          <Shell>{children}</Shell>
        </SessionProvider>
      </body>
    </html>
  );
}
