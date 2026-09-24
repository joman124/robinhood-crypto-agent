/** @type {import('next').NextConfig} */
const nextConfig = {
  // The dashboard holds no Robinhood credentials and makes no outbound calls.
  // Everything it renders was pushed to it by `rhca dashboard-sync`.
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          // The page has Accept buttons: never let another site frame it and
          // steer a click onto one.
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
