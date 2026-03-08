'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const Navigation = () => {
  const pathname = usePathname();

  const links = [
    { href: '/', label: 'Dots Map' },
    { href: '/chloropleth', label: 'Heat Map' },
  ];

  return (
    <nav
      style={{
        background: 'linear-gradient(90deg, #0a0a0a 0%, #1a1a2e 50%, #0a0a0a 100%)',
        padding: '0',
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        zIndex: 2000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        height: '52px',
        borderBottom: '1px solid rgba(255,100,50,0.3)',
        boxShadow: '0 2px 20px rgba(255,80,30,0.15)',
      }}
    >
      {/* Brand */}
      <div style={{
        padding: '0 1.5rem',
        display: 'flex',
        alignItems: 'center',
        gap: '0.6rem',
      }}>
        <span style={{ fontSize: '18px' }}>⚡</span>
        <span style={{
          color: '#ff6432',
          fontWeight: 700,
          fontSize: '13px',
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
        }}>
          Odisha Power Outage
        </span>
      </div>

      {/* Links */}
      <div style={{ display: 'flex', height: '100%' }}>
        {links.map(({ href, label }) => {
          const isActive = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              style={{
                color: isActive ? '#ff6432' : 'rgba(255,255,255,0.65)',
                textDecoration: 'none',
                padding: '0 1.4rem',
                height: '100%',
                display: 'flex',
                alignItems: 'center',
                fontSize: '13px',
                fontWeight: isActive ? 600 : 400,
                letterSpacing: '0.05em',
                borderBottom: isActive ? '2px solid #ff6432' : '2px solid transparent',
                transition: 'all 0.2s',
              }}
            >
              {label}
            </Link>
          );
        })}
      </div>
    </nav>
  );
};

export default Navigation;
