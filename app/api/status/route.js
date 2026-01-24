import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { systemStatus } from "@/shared/schema";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const [row] = await db.select().from(systemStatus).limit(1);

  const fallback = {
    id: 0,
    markets: "BTCUSDT, ETHUSDT",
    openPositions: 0,
    survival: "NORMAL",
    equity: "0.00",
    lastHeartbeat: new Date(),
  };

  return NextResponse.json(row ?? fallback, { status: 200 });
}
