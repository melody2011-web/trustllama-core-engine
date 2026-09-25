import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { sendApprovedWebhookArchive } from '../operator-webhook-download';

test('operator Webhook Pro download serves only the approved manifest and bytes', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'operator-webhook-'));
  const filename = 'solana-webhook-core-rail-pro.zip';
  const releasePath = join(directory, filename);
  const manifestPath = join(directory, 'approved-release.json');
  const bytes = Buffer.from('approved fixture archive');
  const digest = createHash('sha256').update(bytes).digest('hex');
  const approve = (sha256: string, name = filename) =>
    writeFileSync(manifestPath, JSON.stringify({ filename: name, sha256 }));
  writeFileSync(releasePath, bytes);
  const server = createServer((_request, response) =>
    sendApprovedWebhookArchive(response, releasePath, filename));
  await new Promise<void>((done) => server.listen(0, '127.0.0.1', done));
  try {
    const address = server.address();
    assert(address && typeof address !== 'string');
    const url = `http://127.0.0.1:${address.port}`;
    assert.equal((await fetch(url)).status, 503, 'missing manifest fails closed');
    approve('0'.repeat(64));
    assert.equal((await fetch(url)).status, 503, 'mismatched checksum fails closed');
    approve(digest, 'other.zip');
    assert.equal((await fetch(url)).status, 503, 'mismatched filename fails closed');
    approve(digest);
    const approved = await fetch(url);
    assert.equal(approved.status, 200);
    assert.equal(approved.headers.get('content-type'), 'application/zip');
    assert.equal(approved.headers.get('content-disposition'), `attachment; filename="${filename}"`);
    assert.deepEqual(Buffer.from(await approved.arrayBuffer()), bytes);
    writeFileSync(releasePath, 'changed archive');
    assert.equal((await fetch(url)).status, 503, 'changed bytes fail closed');
  } finally {
    server.closeAllConnections();
    await new Promise<void>((done) => server.close(done));
    rmSync(directory, { recursive: true, force: true });
  }
});