import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { vaultState } from "@/shared/schema";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const [state] = await db.select().from(vaultState).limit(1);

  return NextResponse.json(
    state ?? {
      isPinSet: false,
      isLocked: true,
      isEnabled: true,
    }
  );
}
