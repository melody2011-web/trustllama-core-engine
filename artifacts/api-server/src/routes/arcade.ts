import { createHash, timingSafeEqual } from "node:crypto";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { Router, type IRouter } from "express";
import {
  GetArcadeDashboardHeader,
  GetArcadeDashboardResponse,
} from "@workspace/api-zod";

const router: IRouter = Router();

type PlayerRow = {
  playerId: string;
  name: string;
  highScore: number;
  ownershipHash: string | null;
  matchesPlayed: number;
};

type TicketRow = {
  ticketId: string;
  status: "simulated_active";
  entryFeeTlama: number;
  issuedAt: number;
};

type MatchRow = {
  ticketId: string;
  finalScore: number;
  entryFeeTlama: number;
  completedAt: number;
};

function databasePath(): string {
  return (
    process.env["TLAMA_GAME_DB_PATH"]?.trim() ||
    resolve(
      import.meta.dirname,
      "../../../llama_website/llama_game/data/tlama_arcade.sqlite3",
    )
  );
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function ownsPlayer(savedHash: string | null, ownershipToken: string): boolean {
  if (!savedHash) return false;
  const actual = Buffer.from(savedHash, "hex");
  const expected = Buffer.from(sha256(ownershipToken), "hex");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function isoTimestamp(seconds: number): string {
  return new Date(seconds * 1000).toISOString();
}

router.get("/arcade/dashboard", (req, res): void => {
  const params = GetArcadeDashboardHeader.safeParse({
    "X-Tlama-Player-Key": req.get("X-Tlama-Player-Key"),
    "X-Tlama-Ownership-Token": req.get("X-Tlama-Ownership-Token"),
  });
  if (!params.success) {
    res.status(401).json({ ok: false, error: "Arcade identity is missing or invalid." });
    return;
  }

  const playerKey = params.data["X-Tlama-Player-Key"].replaceAll("-", "").toLowerCase();
  const ownershipToken = params.data["X-Tlama-Ownership-Token"];
  const path = databasePath();
  if (!existsSync(path)) {
    res.status(404).json({ ok: false, error: "No Arcade player exists in this browser yet." });
    return;
  }

  let database: DatabaseSync | undefined;
  try {
    database = new DatabaseSync(path, { readOnly: true });
    const player = database
      .prepare(
        `SELECT
          player_id AS playerId,
          name,
          high_score AS highScore,
          ownership_hash AS ownershipHash,
          (
            SELECT COUNT(*)
            FROM match_tickets
            WHERE match_tickets.player_id = players.player_id
              AND match_tickets.status = 'simulated_completed'
          ) AS matchesPlayed
        FROM players
        WHERE identity_hash = ?`,
      )
      .get(sha256(playerKey)) as PlayerRow | undefined;

    if (!player) {
      res.status(404).json({ ok: false, error: "No Arcade player exists in this browser yet." });
      return;
    }
    if (!ownsPlayer(player.ownershipHash, ownershipToken)) {
      res.status(401).json({ ok: false, error: "Arcade player ownership could not be verified." });
      return;
    }

    const activeTickets = database
      .prepare(
        `SELECT
          ticket_id AS ticketId,
          status,
          entry_fee_tlama AS entryFeeTlama,
          issued_at AS issuedAt
        FROM match_tickets
        WHERE player_id = ? AND status = 'simulated_active'
        ORDER BY issued_at DESC`,
      )
      .all(player.playerId) as TicketRow[];
    const matchHistory = database
      .prepare(
        `SELECT
          ticket_id AS ticketId,
          final_score AS finalScore,
          entry_fee_tlama AS entryFeeTlama,
          completed_at AS completedAt
        FROM match_tickets
        WHERE player_id = ? AND status = 'simulated_completed'
          AND final_score IS NOT NULL AND completed_at IS NOT NULL
        ORDER BY completed_at DESC
        LIMIT 50`,
      )
      .all(player.playerId) as MatchRow[];

    res.json(
      GetArcadeDashboardResponse.parse({
        player: {
          playerId: player.playerId,
          name: player.name,
          highScore: player.highScore,
          matchesPlayed: player.matchesPlayed,
        },
        activeTickets: activeTickets.map((ticket) => ({
          ...ticket,
          issuedAt: isoTimestamp(ticket.issuedAt),
        })),
        matchHistory: matchHistory.map((match) => ({
          ...match,
          completedAt: isoTimestamp(match.completedAt),
        })),
        paymentsEnabled: false,
      }),
    );
  } catch (error) {
    req.log.error({ error }, "Arcade dashboard storage read failed");
    res.status(503).json({ ok: false, error: "Arcade dashboard data is temporarily unavailable." });
  } finally {
    database?.close();
  }
});

export default router;