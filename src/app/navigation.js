'use client';

import Link from 'next/link';

const Navigation = () => {
  return (
    <nav
      style={{
        backgroundColor: '#1b1b1b',
        padding: '1rem',
        position: 'fixed',
        top: 0,
        width: '100%',
        zIndex: 1000,
        display: 'flex',
        justifyContent: 'center',
        gap: '2rem'
      }}
    >
      <Link
        href="/"
        style={{
          color: '#fff',
          textDecoration: 'none',
          padding: '0.5rem 1rem',
          borderRadius: '4px',
          transition: 'background-color 0.3s'
        }}
      >
        Dots Map
      </Link>
      <Link
        href="/chloropleth"
        style={{
          color: '#fff',
          textDecoration: 'none',
          padding: '0.5rem 1rem',
          borderRadius: '4px',
          transition: 'background-color 0.3s'
        }}
      >
        Heat Map
      </Link>
    </nav>
  );
};

export default Navigation;
