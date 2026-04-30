/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The dev server proxies /api to FastAPI on port 8000.
  async rewrites() {
    return [{ source: "/api/:path*", destination: "http://localhost:8000/:path*" }];
  },
};
export default nextConfig;
