/**
 * Cloudflare Worker — External 24/7 Keep-Alive Pinger for Render
 *
 * How to deploy (100% Free, takes 1 minute):
 *   1. Go to https://dash.cloudflare.com -> Workers & Pages -> Create Worker
 *   2. Paste this code and click "Deploy"
 *   3. Go to Worker Settings -> Variables -> Add `RENDER_APP_URL` = "https://your-app.onrender.com"
 *   4. Go to Worker Settings -> Triggers -> Cron Triggers -> Add `*/5 * * * *` (every 5 minutes)
 */

export default {
  async scheduled(event, env, ctx) {
    const baseUrl = (env.RENDER_APP_URL || "").replace(/\/$/, "");
    if (!baseUrl) {
      console.log("RENDER_APP_URL not configured in Worker env vars.");
      return;
    }

    const endpoints = ["/keep-alive", "/health"];
    const target = `${baseUrl}${endpoints[Math.floor(Math.random() * endpoints.length)]}`;

    try {
      const response = await fetch(target, {
        method: "GET",
        headers: {
          "User-Agent": "CloudflareWorker-KeepAlive/1.0",
          "Cache-Control": "no-cache",
        },
      });
      console.log(`[Keep-Alive] Pinged ${target} -> HTTP ${response.status}`);
    } catch (err) {
      console.error(`[Keep-Alive] Failed to ping ${target}:`, err);
    }
  },

  async fetch(request, env, ctx) {
    return new Response(
      JSON.stringify({ status: "Cloudflare Keep-Alive Worker Active" }),
      { headers: { "Content-Type": "application/json" } }
    );
  },
};
