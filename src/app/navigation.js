'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { BASE_PATH } from './config';
import { useTheme } from './theme';

const LINKS = [
  { href: '/', label: 'Home' },
  { href: '/live', label: 'Live NRT' },
  { href: '/reliability', label: 'Reliability' },
];

/**
 * Normal is compared against the clean route path. usePathname may or may not
 * include the deployment basePath depending on the Next.js runtime, so strip it
 * before comparing and tolerate a trailing slash.
 */
function cleanPathname(pathname) {
  let path = pathname || '/';
  const base = BASE_PATH || '';
  if (base && (path === base || path.startsWith(`${base}/`))) {
    path = path.slice(base.length) || '/';
  }
  if (path.length > 1) path = path.replace(/\/+$/, '');
  return path || '/';
}

function BoltIcon() {
  return (
    <svg
      className="brand-mark"
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M13.1 2 4.5 13.6h5.9L9.2 22l8.7-11.9h-5.9L13.1 2Z" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg
      className="theme-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.6v2.1M12 19.3v2.1M2.6 12h2.1M19.3 12h2.1M5.4 5.4l1.5 1.5M17.1 17.1l1.5 1.5M18.6 5.4l-1.5 1.5M6.9 17.1l-1.5 1.5" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      className="theme-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M20.1 14.6A8.1 8.1 0 0 1 9.4 3.9a8.1 8.1 0 1 0 10.7 10.7Z" />
    </svg>
  );
}

const Navigation = () => {
  const { theme, toggle } = useTheme();
  const pathname = usePathname();
  const current = cleanPathname(pathname);
  const isDark = theme === 'dark';

  return (
    <nav className="top-nav" aria-label="Primary">
      <div className="nav-brand">
        <span className="nav-brand-mark" aria-hidden="true">
          <BoltIcon />
        </span>
        <span className="nav-brand-label">Odisha Power Outage</span>
        <span className="nav-tagline">Live grid intelligence</span>
      </div>

      <div className="nav-links">
        {LINKS.map((link) => {
          const active = current === link.href;
          return (
            <Link
              key={link.href}
              href={link.href}
              className={active ? 'nav-link active' : 'nav-link'}
              aria-current={active ? 'page' : undefined}
            >
              {link.label}
            </Link>
          );
        })}
      </div>

      <div className="nav-actions">
        <button
          className="theme-toggle"
          type="button"
          onClick={toggle}
          aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
          aria-pressed={isDark}
          title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
        >
          {isDark ? <SunIcon /> : <MoonIcon />}
        </button>
      </div>
    </nav>
  );
};

export default Navigation;
