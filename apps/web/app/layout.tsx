import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Continuity",
  description:
    "Autonomous integration reliability — monitor provider changes, prove impact, deliver validated migrations.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-neutral-50 text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-100">
        <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-6">
          <header className="flex items-center justify-between border-b border-neutral-200 py-5 dark:border-neutral-800">
            <span className="text-sm font-semibold tracking-tight">
              Continuity
            </span>
            <span className="text-xs text-neutral-500 dark:text-neutral-400">
              Integration reliability
            </span>
          </header>
          <main className="flex-1 py-10">{children}</main>
          <footer className="border-t border-neutral-200 py-5 text-xs text-neutral-500 dark:border-neutral-800 dark:text-neutral-400">
            Phase 1 — backend foundation. Not yet a working product.
          </footer>
        </div>
      </body>
    </html>
  );
}
