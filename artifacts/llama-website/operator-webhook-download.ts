import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { ServerResponse } from 'node:http';
import path from 'node:path';

// This module runs only in Vite's Node server; it is not imported by the client.
export function sendApprovedWebhookArchive(
  response: ServerResponse,
  releasePath: string,
  filename: string,
): void {
  try {
    const manifest: unknown = JSON.parse(
      readFileSync(path.resolve(path.dirname(releasePath), 'approved-release.json'), 'utf8'),
    );
    if (
      !manifest ||
      typeof manifest !== 'object' ||
      !('filename' in manifest) ||
      manifest.filename !== filename ||
      !('sha256' in manifest) ||
      typeof manifest.sha256 !== 'string' ||
      !/^[a-f0-9]{64}$/.test(manifest.sha256)
    ) {
      response.statusCode = 503;
      response.end('The validated Webhook Pro archive is unavailable.');
      return;
    }
    // Hash the exact bytes sent, rather than relying on file metadata.
    const archive = readFileSync(releasePath);
    if (!archive.length || createHash('sha256').update(archive).digest('hex') !== manifest.sha256) {
      response.statusCode = 503;
      response.end('The validated Webhook Pro archive is unavailable.');
      return;
    }
    response.statusCode = 200;
    response.setHeader('Content-Type', 'application/zip');
    response.setHeader('Content-Length', archive.length);
    response.setHeader('Content-Disposition', `attachment; filename="${filename}"`);
    response.end(archive);
  } catch {
    response.statusCode = 503;
    response.end('The validated Webhook Pro archive is unavailable.');
  }
}