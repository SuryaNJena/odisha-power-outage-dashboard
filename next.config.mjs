/** @type {import('next').NextConfig} */

const isDeploy = process.env.NEXT_PUBLIC_USE_BASE_PATH === 'true';
const basePath = isDeploy ? (process.env.NEXT_PUBLIC_BASE_PATH || '/odisha-power-outage-dashboard') : '';

const nextConfig = {
  output: 'export',          // static export for GitHub Pages
  basePath,
  assetPrefix: basePath,
  // GitHub Pages serves directory indexes, not /live.html. Only the Pages build
  // gets trailing slashes; the self-hosted Go server keeps /live.html.
  trailingSlash: isDeploy,
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
