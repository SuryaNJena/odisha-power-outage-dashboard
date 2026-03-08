/** @type {import('next').NextConfig} */

const isProd = process.env.NODE_ENV === 'production';
// Set GITHUB_REPO_NAME in the Actions environment, or it defaults to the repo name
const basePath = isProd ? (process.env.NEXT_PUBLIC_BASE_PATH || '/odisha-power-outage-dashboard') : '';

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
