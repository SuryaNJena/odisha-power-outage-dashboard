/** @type {import('next').NextConfig} */

const isDeploy = process.env.NEXT_PUBLIC_USE_BASE_PATH === 'true';
const basePath = isDeploy ? (process.env.NEXT_PUBLIC_BASE_PATH || '/odisha-power-outage-dashboard') : '';

const nextConfig = {
  output: 'export',          // static export for GitHub Pages
  basePath,
  assetPrefix: basePath,
  images: {
    unoptimized: true,       // required for static export
  },
  devIndicators: {
    appIsrStatus: false,
  },
  // Make basePath available in client components
  env: {
    NEXT_PUBLIC_BASE_PATH: basePath,
  },
};

export default nextConfig;
