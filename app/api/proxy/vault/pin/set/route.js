export const runtime = "nodejs";

import { proxyFetch } from "../../../_lib";

export async function POST(req) {
  return proxyFetch(req, "/vault/pin/set", "POST");
}

export async function OPTIONS() {
  return new Response(null, {
    status: 204,
    headers: {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
      "access-control-allow-headers": "*",
    },
  });
}
