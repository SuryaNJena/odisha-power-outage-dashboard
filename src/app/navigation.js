'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { BASE_PATH } from './config';
import { useTheme } from './theme';

const LINKS = [
  { href: '/', label: 'Home', icon: '🗺️' },
  { href: '/live', label: 'Live NRT', icon: '📡' },
  { href: '/reliability', label: 'Reliability', icon: '📊' },
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

const Navigation = () => {
  const { theme, toggle } = useTheme();
  const pathname = usePathname();
  const current = cleanPathname(pathname);

  return (
    <nav
      style={{
        background: 'var(--bg-elev)',
        borderBottom: '1px solid var(--border)',
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        zIndex: 2000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 8,
        height: 56,
        paddingRight: 16,
        backdropFilter: 'blur(10px)',
      }}
    >
      {/* Brand */}
      <div
        style={{
          padding: '0 1.1rem',
          display: 'flex',
          alignItems: 'center',
          gap: '0.55rem',
          flex: '0 0 auto',
          minWidth: 0,
        }}
      >
        <span style={{ fontSize: 20 }} aria-hidden="true">⚡</span>
        <span
          className="nav-brand-label"
          style={{
            color: 'var(--accent)',
            fontWeight: 800,
            fontSize: 13,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
            whiteSpace: 'nowrap',
          }}
        >
          Odisha Power Outage
        </span>
        <span
          className="nav-tagline"
          style={{
            color: 'var(--text-muted)',
            fontSize: 11,
            fontWeight: 500,
            letterSpacing: '0.03em',
            borderLeft: '1px solid var(--border)',
            paddingLeft: 10,
            whiteSpace: 'nowrap',
          }}
        >
          Live grid intelligence
        </span>
      </div>

      {/* Primary navigation — horizontally scrollable on narrow screens */}
      <div className="nav-links" aria-label="Primary navigation">
        {LINKS.map((link) => {
          const active = current === link.href;
          return (
            <Link
              key={link.href}
              href={link.href}
              className={active ? 'nav-link active' : 'nav-link'}
              aria-current={active ? 'page' : undefined}
            >
              <span aria-hidden="true">{link.icon}</span>
              <span>{link.label}</span>
            </Link>
          );
        })}
      </div>

      {/* Theme toggle */}
      <button
        className="theme-toggle"
        type="button"
        onClick={toggle}
        aria-label="Toggle theme"
        title="Toggle light / dark mode"
        style={{ flex: '0 0 auto' }}
      >
        {theme === 'dark' ? '☀️' : '🌙'}
      </button>
    </nav>
  );
};

export default Navigation;
