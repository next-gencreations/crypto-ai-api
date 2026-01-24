import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { vaultState, verifyPinSchema } from "@/shared/schema";
import { eq } from "drizzle-orm";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req) {
  try {
    const body = await req.json();
    const { pin } = verifyPinSchema.parse(body);

    const [state] = await db.select().from(vaultState).limit(1);

    if (!state?.isPinSet) {
      return NextResponse.json({ message: "PIN not set" }, { status: 400 });
    }

    if (state.pinHash !== pin) {
      return NextResponse.json({ message: "Invalid PIN" }, { status: 401 });
    }

    await db.update(vaultState).set({ isLocked: false }).where(eq(vaultState.id, state.id));

    return NextResponse.json({ success: true, message: "Unlocked" }, { status: 200 });
  } catch (err) {
    return NextResponse.json({ message: "Error" }, { status: 500 });
  }
}
