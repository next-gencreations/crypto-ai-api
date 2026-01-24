import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { vaultState } from "@/shared/schema";
import { eq } from "drizzle-orm";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST() {
  const [state] = await db.select().from(vaultState).limit(1);

  if (state) {
    await db.update(vaultState).set({ isLocked: true }).where(eq(vaultState.id, state.id));
  } else {
    await db.insert(vaultState).values({ isLocked: true, isPinSet: false, isEnabled: true });
  }

  return NextResponse.json({ success: true }, { status: 200 });
}
