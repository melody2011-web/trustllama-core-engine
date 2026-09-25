import { Router, type IRouter } from "express";
import healthRouter from "./health";
import paperRouter from "./paper";
import telegramRouter from "./telegram";
import arcadeRouter from "./arcade";
import feedbackRouter from "./feedback";
import licensingRouter from "./licensing";

const router: IRouter = Router();

router.use(healthRouter);
router.use(paperRouter);
router.use(telegramRouter);
router.use(arcadeRouter);
router.use(feedbackRouter);
router.use(licensingRouter);

export default router;
