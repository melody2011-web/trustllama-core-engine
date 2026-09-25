import {
  bigint,
  pgTable,
  text,
  timestamp,
  uuid,
} from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const licenseCheckoutIntentsTable = pgTable(
  "license_checkout_intents",
  {
    id: uuid("id").primaryKey(),
    reference: text("reference").notNull().unique(),
    tier: text("tier").notNull(),
    amountMicroUsdc: bigint("amount_micro_usdc", { mode: "number" }).notNull(),
    status: text("status").notNull().default("pending"),
    signature: text("signature").unique(),
    checkoutTokenHash: text("checkout_token_hash"),
    downloadTokenHash: text("download_token_hash").unique(),
    createdAt: timestamp("created_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    verifiedAt: timestamp("verified_at", { withTimezone: true }),
    downloadExpiresAt: timestamp("download_expires_at", { withTimezone: true }),
    downloadClaimedAt: timestamp("download_claimed_at", { withTimezone: true }),
    downloadedAt: timestamp("downloaded_at", { withTimezone: true }),
  },
);

export const insertLicenseCheckoutIntentSchema = createInsertSchema(
  licenseCheckoutIntentsTable,
).omit({
  createdAt: true,
  verifiedAt: true,
  downloadExpiresAt: true,
  downloadedAt: true,
});

export type InsertLicenseCheckoutIntent = z.infer<
  typeof insertLicenseCheckoutIntentSchema
>;
export type LicenseCheckoutIntent =
  typeof licenseCheckoutIntentsTable.$inferSelect;