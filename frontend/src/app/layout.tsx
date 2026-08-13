import type { Metadata, Viewport } from "next";
import { DM_Sans, Manrope } from "next/font/google";
import { FocusModality } from "@/components/ui/focus-modality";
import { Providers } from "@/components/providers";
import "./globals.css";

const body = DM_Sans({ variable: "--font-body", subsets: ["latin"], preload: false });
const display = Manrope({ variable: "--font-display", subsets: ["latin"], preload: false });

export const metadata: Metadata = {
  title: "fyn AI",
  description: "A conversational personal finance workspace that turns natural language into structured financial truth.",
};

export const viewport: Viewport = {
  themeColor: "#f4f4ef",
  // The composer is pinned to the bottom; let the mobile keyboard shrink the
  // layout instead of covering it.
  interactiveWidget: "resizes-content",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" className={`${body.variable} ${display.variable} h-full antialiased`}><body className="min-h-full">
    <FocusModality />
    <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-100 focus:rounded-lg focus:bg-secondary focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-on-secondary">Skip to main content</a>
    <Providers>{children}</Providers>
  </body></html>;
}
