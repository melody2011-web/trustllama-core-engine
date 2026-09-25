import { randomUUID } from "node:crypto";
import { db, playerFeedbackTable } from "@workspace/db";
import {
  SubmitFeedbackBody,
  SubmitFeedbackResponse,
} from "@workspace/api-zod";
import { Router, type IRouter, type Request, type Response } from "express";
import { ipKeyGenerator, rateLimit } from "express-rate-limit";
import { PostgresRateLimitStore } from "../middleware/postgres-rate-limit-store";

const router: IRouter = Router();
const FEEDBACK_WINDOW_MS = 15 * 60 * 1000;
const FEEDBACK_LIMIT = 5;
const GAME_TWO_PAGE = "/retro-arcade/";

function feedbackStore(): PostgresRateLimitStore | undefined {
  if (process.env.NODE_ENV !== "production") return undefined;
  const store = new PostgresRateLimitStore();
  store.prefix = "feedback:";
  return store;
}

const feedbackRateLimiter = rateLimit({
  windowMs: FEEDBACK_WINDOW_MS,
  limit: FEEDBACK_LIMIT,
  standardHeaders: "draft-8",
  legacyHeaders: false,
  store: feedbackStore(),
  keyGenerator: (req) =>
    ipKeyGenerator(req.ip ?? req.socket.remoteAddress ?? "unknown"),
  handler: (req: Request, res: Response) => {
    req.log.warn({ path: req.path }, "Feedback rate limit exceeded");
    res.status(429).json({
      ok: false,
      error: "Too many feedback submissions. Please try again later.",
    });
  },
});

function normalizeMessage(message: string): string {
  return message
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .trim();
}

router.post(
  "/feedback",
  feedbackRateLimiter,
  async (req, res): Promise<void> => {
    const parsed = SubmitFeedbackBody.safeParse(req.body);
    if (!parsed.success) {
      res.status(400).json({ ok: false, error: "Invalid feedback submission." });
      return;
    }

    const message = normalizeMessage(parsed.data.message);
    if (!message || parsed.data.page !== GAME_TWO_PAGE) {
      res.status(400).json({ ok: false, error: "Invalid feedback submission." });
      return;
    }

    const id = randomUUID();
    try {
      await db.insert(playerFeedbackTable).values({
        id,
        category: parsed.data.category,
        message,
        page: GAME_TWO_PAGE,
        status: "new",
      });
    } catch (error) {
      req.log.error(
        { feedbackId: id, error },
        "Feedback persistence failed",
      );
      res.status(503).json({
        ok: false,
        error: "Feedback service is temporarily unavailable.",
      });
      return;
    }

    req.log.info({ feedbackId: id }, "Feedback accepted");
    res.status(202).json(SubmitFeedbackResponse.parse({ ok: true, id }));
  },
);

export default router;