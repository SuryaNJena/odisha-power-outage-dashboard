'use client';

import { useTheme } from './theme';

const Navigation = () => {
  const { theme, toggle } = useTheme();

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
        height: 56,
        backdropFilter: 'blur(10px)',
      }}
    >
      {/* Brand */}
      <div
        style={{
          padding: '0 1.25rem',
          display: 'flex',
          alignItems: 'center',
          gap: '0.55rem',
        }}
      >
        <span style={{ fontSize: 20 }}>⚡</span>
        <span
          style={{
            color: 'var(--accent)',
            fontWeight: 800,
            fontSize: 13,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
          }}
        >
          Odisha Power Outage
        </span>
        <span
          style={{
            color: 'var(--text-muted)',
            fontSize: 11,
            fontWeight: 500,
            letterSpacing: '0.03em',
            borderLeft: '1px solid var(--border)',
            paddingLeft: 10,
          }}
        >
          Dots + Heat map
        </span>
      </div>

      {/* Theme toggle */}
      <button
        className="theme-toggle"
        style={{ marginRight: 16 }}
        onClick={toggle}
        aria-label="Toggle theme"
        title="Toggle light / dark mode"
      >
        {theme === 'dark' ? '☀️' : '🌙'}
      </button>
    </nav>
  );
};

export default Navigation;


