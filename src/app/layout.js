import { PlatformProvider } from './platformMode';
import { Fira_Sans, Fira_Mono } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "./theme";

// Fira Sans is the open-source humanist sans commissioned by Mozilla and drawn
// by Erik Spiekermann's studio (Edenspiekermann, with Carrois). Fira Mono is its
// companion for figures and code. Using the family as a system — one voice,
// restrained weights, tabular figures — follows Spiekermann's typographic
// approach rather than mixing display faces.
const firaSans = Fira_Sans({
  variable: "--font-fira-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  style: ["normal"],
  display: "swap",
  adjustFontFallback: true,
  fallback: ["system-ui", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
});

const firaMono = Fira_Mono({
  variable: "--font-fira-mono",
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  display: "swap",
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
});

const basePath = process.env.NEXT_PUBLIC_BASE_PATH || '';

export const metadata = {
  icons: {
    icon: [
      { url: `${basePath}/favicon.ico`, sizes: "any" },
      { url: `${basePath}/favicon-32x32.png`, type: "image/png", sizes: "32x32" },
      { url: `${basePath}/favicon-16x16.png`, type: "image/png", sizes: "16x16" },
    ],
    apple: { url: `${basePath}/apple-touch-icon.png`, sizes: "180x180" },
  },
  title: "Odisha Live Power Outage",
  description: "Made with ❤️ in Odisha",
};

// Set the theme before hydration to avoid a flash of the wrong theme.
const themeInitScript = `(function(){try{var t=localStorage.getItem('odisha-theme');if(!t){t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();`;

export default function RootLayout({ children }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body className={`${firaSans.variable} ${firaMono.variable}`}>
        <ThemeProvider><PlatformProvider>{children}</PlatformProvider></ThemeProvider>
      </body>
    </html>
  );
}
