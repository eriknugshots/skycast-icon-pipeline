// publish/texdev.mjs — publish the cloud-texture dev feed (texdev_build.py's
// output) under texdev/ in the ICON feed's Blob store; see texdevplan.mjs.
//
// Usage: BLOB_READ_WRITE_TOKEN=… node publish/texdev.mjs <outDir>
//   (<outDir> holds texdev/ and nothing else)
import { put, list, del } from '@vercel/blob';
import { createRetry } from './pace.mjs';
import { publishTexdev } from './texdevplan.mjs';

async function main() {
  const [outDir] = process.argv.slice(2);
  if (!outDir) throw new Error('usage: node publish/texdev.mjs <outDir>');
  if (!process.env.BLOB_READ_WRITE_TOKEN) throw new Error('BLOB_READ_WRITE_TOKEN is not set');
  await publishTexdev({ outDir, put, list, del, retry: createRetry({ attempts: 4 }) });
}

main().catch((e) => { console.error(`::error::texdev publish failed: ${e.message || e}`); process.exit(1); });
