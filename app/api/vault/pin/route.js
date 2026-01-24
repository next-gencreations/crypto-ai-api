import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { vaultState, setPinSchema } from "@/shared/schema";
import { eq } from "drizzle-orm";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req) {
  try {
    const body = await req.json();
    const { pin } = setPinSchema.parse(body);

    const [existing] = await db.select().from(vaultState).limit(1);

    if (existing) {
      await db
        .update(vaultState)
        .set({ isPinSet: true, pinHash: pin, isLocked: true, isEnabled: true })
        .where(eq(vaultState.id, existing.id));
    } else {
      await db.insert(vaultState).values({ isPinSet: true, pinHash: pin, isLocked: true, isEnabled: true });
    }

    return NextResponse.json({ success: true, message: "PIN set" }, { status: 200 });
  } catch (err) {
    return NextResponse.json({ message: "Invalid PIN" }, { status: 400 });
  }
}
