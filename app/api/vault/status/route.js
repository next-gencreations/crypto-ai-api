import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { vaultState } from "@/shared/schema";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const [row] = await db.select().from(vaultState).limit(1);

  return NextResponse.json(
    row ?? { id: 0, isPinSet: false, isLocked: true, pinHash: null, isEnabled: true },
    { status: 200 }
  );
}
