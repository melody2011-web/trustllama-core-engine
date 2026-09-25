import { pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod/v4";

export const playerFeedbackTable = pgTable("player_feedback", {
  id: uuid("id").primaryKey(),
  category: text("category").notNull(),
  message: text("message").notNull(),
  page: text("page").notNull(),
  status: text("status").notNull().default("new"),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});

export const insertPlayerFeedbackSchema = createInsertSchema(
  playerFeedbackTable,
).omit({ createdAt: true });

export type InsertPlayerFeedback = z.infer<typeof insertPlayerFeedbackSchema>;
export type PlayerFeedback = typeof playerFeedbackTable.$inferSelect;