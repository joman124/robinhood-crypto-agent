/** @type {import('next').NextConfig} */
const nextConfig = {
  // The dashboard holds no Robinhood credentials and makes no outbound calls.
  // Everything it renders was pushed to it by `rhca dashboard-sync`.
  reactStrictMode: true,
};

export default nextConfig;
