import { pool } from "@workspace/db";
import type { IncrementResponse, Store } from "express-rate-limit";

const TABLE_NAME = "api_rate_limit_counters";

export class PostgresRateLimitStore implements Store {
  localKeys = false;
  prefix = "api:";
  private windowMs = 0;
  private readonly ready: Promise<void>;

  constructor() {
    this.ready = pool
      .query(`
        CREATE TABLE IF NOT EXISTS ${TABLE_NAME} (
          key text PRIMARY KEY,
          total_hits integer NOT NULL,
          reset_at timestamptz NOT NULL
        )
      `)
      .then(() => undefined);
  }

  init(options: { windowMs: number }): void {
    this.windowMs = options.windowMs;
  }

  async increment(key: string): Promise<IncrementResponse> {
    await this.ready;
    const namespacedKey = `${this.prefix}${key}`;
    const result = await pool.query<{
      total_hits: number;
      reset_at: Date;
    }>(
      `
        INSERT INTO ${TABLE_NAME} (key, total_hits, reset_at)
        VALUES ($1, 1, clock_timestamp() + ($2 * interval '1 millisecond'))
        ON CONFLICT (key) DO UPDATE SET
          total_hits = CASE
            WHEN ${TABLE_NAME}.reset_at <= clock_timestamp() THEN 1
            ELSE ${TABLE_NAME}.total_hits + 1
          END,
          reset_at = CASE
            WHEN ${TABLE_NAME}.reset_at <= clock_timestamp()
              THEN clock_timestamp() + ($2 * interval '1 millisecond')
            ELSE ${TABLE_NAME}.reset_at
          END
        RETURNING total_hits, reset_at
      `,
      [namespacedKey, this.windowMs],
    );

    const counter = result.rows[0];
    if (!counter) {
      throw new Error("Rate-limit counter update returned no row.");
    }

    return {
      totalHits: counter.total_hits,
      resetTime: counter.reset_at,
    };
  }

  async decrement(key: string): Promise<void> {
    await this.ready;
    await pool.query(
      `
        UPDATE ${TABLE_NAME}
        SET total_hits = GREATEST(total_hits - 1, 0)
        WHERE key = $1 AND reset_at > clock_timestamp()
      `,
      [`${this.prefix}${key}`],
    );
  }

  async resetKey(key: string): Promise<void> {
    await this.ready;
    await pool.query(`DELETE FROM ${TABLE_NAME} WHERE key = $1`, [
      `${this.prefix}${key}`,
    ]);
  }

  async resetAll(): Promise<void> {
    await this.ready;
    await pool.query(`DELETE FROM ${TABLE_NAME} WHERE key LIKE $1`, [
      `${this.prefix}%`,
    ]);
  }
}