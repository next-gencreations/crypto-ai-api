import { NextResponse } from "next/server";
import { db } from "@/server/db";
import { bankroll } from "@/shared/schema";
import { insertBankrollSchema } from "@/shared/schema";
import { eq } from "drizzle-orm";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const [row] = await db.select().from(bankroll).limit(1);
  return NextResponse.json(row ?? { id: 0, amount: "1000", currency: "GBP" }, { status: 200 });
}

export async function POST(req) {
  try {
    const body = await req.json();
    const parsed = insertBankrollSchema.parse(body);

    const [existing] = await db.select().from(bankroll).limit(1);

    if (existing) {
      const [updated] = await db
        .update(bankroll)
        .set({ amount: parsed.amount, currency: parsed.currency })
        .where(eq(bankroll.id, existing.id))
        .returning();

      return NextResponse.json(updated, { status: 200 });
    }

    const [created] = await db
      .insert(bankroll)
      .values({ amount: parsed.amount, currency: parsed.currency })
      .returning();

    return NextResponse.json(created, { status: 200 });
  } catch (err) {
    return NextResponse.json({ message: "Invalid amount" }, { status: 400 });
  }
}
