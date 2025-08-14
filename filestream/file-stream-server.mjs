// file-stream-server.mjs
// Minimal HTTP server to stream file contents in small chunks to avoid context overflows.
// Node 18+ ESM
//
// Endpoints:
// - GET /health -> { ok: true }
// - GET /chunk?path=<abs-or-rel>&offset=<byte>&length=<bytes>&encoding=utf8|base64
// - GET /lines?path=<abs-or-rel>&start=<line1>&end=<lineN>
// - GET /stat?path=<abs-or-rel>
//
// Security:
// - Restricts access to FILE_STREAM_ROOTS (comma-separated). Defaults to current working dir.
// - Prevents path traversal (..)
// - Caps length/line counts.

import http from 'node:http';
import { createReadStream, statSync, existsSync } from 'node:fs';
import { resolve, sep, isAbsolute } from 'node:path';
import { createHash } from 'node:crypto';

const PORT = Number(process.env.FILE_STREAM_PORT || 17777);
const MAX_CHUNK = Number(process.env.FILE_STREAM_MAX_CHUNK || 64 * 1024); // 64 KiB
const MAX_LINES = Number(process.env.FILE_STREAM_MAX_LINES || 400);
const DEFAULT_ENCODING = (process.env.FILE_STREAM_DEFAULT_ENCODING || 'utf8');

const ROOTS = (process.env.FILE_STREAM_ROOTS || process.cwd())
  .split(',')
  .map((p) => p.trim())
  .filter(Boolean)
  .map((p) => resolve(p));

function isUnderRoots(absPath) {
  try {
    const norm = resolve(absPath);
    return ROOTS.some((r) => norm.startsWith(r + sep) || norm === r);
  } catch {
    return false;
  }
}

function safeResolve(inputPath) {
  const p = isAbsolute(inputPath) ? inputPath : resolve(process.cwd(), inputPath);
  if (!isUnderRoots(p)) throw new Error('Path not permitted');
  return p;
}

function parseIntSafe(v, def = 0) {
  const n = Number.parseInt(String(v), 10);
  return Number.isFinite(n) && n >= 0 ? n : def;
}

function writeJson(res, code, obj) {
  res.writeHead(code, {
    'content-type': 'application/json; charset=utf-8',
    'access-control-allow-origin': '*',
    'access-control-allow-headers': '*',
    'access-control-allow-methods': 'GET,OPTIONS',
  });
  res.end(JSON.stringify(obj));
}

function sha256File(path) {
  const hash = createHash('sha256');
  const stream = createReadStream(path);
  return new Promise((resolve, reject) => {
    stream.on('data', (d) => hash.update(d));
    stream.on('error', reject);
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

async function handleChunk(req, res, url) {
  const encoding = (url.searchParams.get('encoding') || DEFAULT_ENCODING).toLowerCase();
  const offset = parseIntSafe(url.searchParams.get('offset'), 0);
  let length = parseIntSafe(url.searchParams.get('length'), MAX_CHUNK);
  if (length <= 0 || length > MAX_CHUNK) length = MAX_CHUNK;

  const rawPath = url.searchParams.get('path');
  if (!rawPath) return writeJson(res, 400, { error: { message: 'Missing path' } });
  let absPath;
  try { absPath = safeResolve(rawPath); } catch (e) {
    return writeJson(res, 403, { error: { message: e.message } });
  }
  if (!existsSync(absPath)) return writeJson(res, 404, { error: { message: 'Not found' } });

  const st = statSync(absPath);
  const end = Math.min(st.size - 1, offset + length - 1);
  if (offset >= st.size) {
    return writeJson(res, 200, {
      path: absPath,
      size: st.size,
      offset,
      bytes: 0,
      eof: true,
      data: encoding === 'base64' ? '' : '',
    });
  }

  const stream = createReadStream(absPath, { start: offset, end });
  const chunks = [];
  stream.on('data', (d) => chunks.push(d));
  stream.on('error', (err) => writeJson(res, 500, { error: { message: String(err) } }));
  stream.on('end', () => {
    const buf = Buffer.concat(chunks);
    const out = encoding === 'base64' ? buf.toString('base64') : buf.toString('utf8');
    writeJson(res, 200, {
      path: absPath,
      size: st.size,
      offset,
      bytes: buf.length,
      next_offset: end + 1,
      eof: end + 1 >= st.size,
      encoding,
      data: out,
    });
  });
}

async function handleLines(req, res, url) {
  const start = parseIntSafe(url.searchParams.get('start'), 1);
  const end = parseIntSafe(url.searchParams.get('end'), start + MAX_LINES - 1);
  let maxEnd = start + MAX_LINES - 1;
  if (end < start) maxEnd = start + MAX_LINES - 1; else maxEnd = Math.min(end, start + MAX_LINES - 1);

  const rawPath = url.searchParams.get('path');
  if (!rawPath) return writeJson(res, 400, { error: { message: 'Missing path' } });
  let absPath;
  try { absPath = safeResolve(rawPath); } catch (e) {
    return writeJson(res, 403, { error: { message: e.message } });
  }
  if (!existsSync(absPath)) return writeJson(res, 404, { error: { message: 'Not found' } });

  const st = statSync(absPath);
  // Read approx by scanning; for simplicity read a capped chunk around expected size (lines up to MAX_LINES, assume avg 200 bytes/line)
  const approxBytes = Math.min(st.size, MAX_LINES * 200);
  const stream = createReadStream(absPath, { start: 0, end: approxBytes - 1 });
  const chunks = [];
  stream.on('data', (d) => chunks.push(d));
  stream.on('error', (err) => writeJson(res, 500, { error: { message: String(err) } }));
  stream.on('end', () => {
    const text = Buffer.concat(chunks).toString('utf8');
    const lines = text.split(/\r?\n/);
    const from = Math.max(1, start);
    const to = Math.min(lines.length, maxEnd);
    const slice = lines.slice(from - 1, to);
    writeJson(res, 200, {
      path: absPath,
      size: st.size,
      start: from,
      end: to,
      lines: slice,
      next_start: to + 1,
      eof: to >= lines.length,
    });
  });
}

async function handleStat(req, res, url) {
  const rawPath = url.searchParams.get('path');
  if (!rawPath) return writeJson(res, 400, { error: { message: 'Missing path' } });
  let absPath;
  try { absPath = safeResolve(rawPath); } catch (e) {
    return writeJson(res, 403, { error: { message: e.message } });
  }
  if (!existsSync(absPath)) return writeJson(res, 404, { error: { message: 'Not found' } });

  const st = statSync(absPath);
  const wantHash = url.searchParams.get('hash') === 'sha256';
  const out = { path: absPath, size: st.size, mtimeMs: st.mtimeMs };
  if (wantHash) {
    try { out.sha256 = await sha256File(absPath); } catch (e) { out.sha256_error = String(e); }
  }
  writeJson(res, 200, out);
}

function writeText(res, code, text, contentType = 'text/plain; charset=utf-8') {
  res.writeHead(code, {
    'content-type': contentType,
    'access-control-allow-origin': '*',
    'access-control-allow-headers': '*',
    'access-control-allow-methods': 'GET,OPTIONS,POST',
  });
  res.end(text);
}

async function handleSearch(req, res, url) {
  const rawPath = url.searchParams.get('path');
  const q = url.searchParams.get('q') || '';
  const isRegex = ['1','true','yes'].includes(String(url.searchParams.get('regex')||'').toLowerCase());
  const icase = ['1','true','yes','i'].includes(String(url.searchParams.get('i')||'').toLowerCase());
  const maxResults = Math.max(1, parseIntSafe(url.searchParams.get('max'), 200));

  if (!rawPath) return writeJson(res, 400, { error: { message: 'Missing path' } });
  if (!q) return writeJson(res, 400, { error: { message: 'Missing q' } });

  let absPath;
    if (url.pathname === '/search') return handleSearch(req, res, url);
    if (url.pathname === '/robots.txt') return writeText(res, 200, 'User-agent: *\nDisallow: /', 'text/plain; charset=utf-8');
    if (url.pathname === '/') return writeJson(res, 200, { ok: true, service: 'file-stream-server', port: PORT, roots: ROOTS, limits: { MAX_CHUNK, MAX_LINES }, endpoints: ['/', '/health', '/stat', '/chunk', '/lines', '/search', '/robots.txt'] });

  try { absPath = safeResolve(rawPath); } catch (e) {
    return writeJson(res, 403, { error: { message: e.message } });
  }
  if (!existsSync(absPath)) return writeJson(res, 404, { error: { message: 'Not found' } });

  const st = statSync(absPath);
  const MAX_SEARCH_BYTES = Number(process.env.FILE_STREAM_MAX_SEARCH_BYTES || 2 * 1024 * 1024); // 2 MiB
  const end = Math.min(st.size - 1, MAX_SEARCH_BYTES - 1);

  const stream = createReadStream(absPath, { start: 0, end });
  const results = [];
  let buf = '';
  let regex;
  if (isRegex) {
    try { regex = new RegExp(q, icase ? 'i' : ''); } catch (e) { return writeJson(res, 400, { error: { message: 'Bad regex' } }); }
  }

  stream.on('data', (d) => { buf += d.toString('utf8'); });
  stream.on('error', (err) => writeJson(res, 500, { error: { message: String(err) } }));
  stream.on('end', () => {
    const lines = buf.split(/\r?\n/);
    for (let i = 0; i < lines.length && results.length < maxResults; i++) {
      const line = lines[i];
      if (isRegex) {
        const m = line.match(regex);
        if (m) {
          results.push({ line: i+1, col: (m.index||0)+1, match: m[0], preview: line });
        }
      } else {
        const hay = icase ? line.toLowerCase() : line;
        const needle = icase ? q.toLowerCase() : q;
        const idx = hay.indexOf(needle);
        if (idx !== -1) results.push({ line: i+1, col: idx+1, match: q, preview: line });
      }
    }
    writeJson(res, 200, {
      path: absPath,
      size: st.size,
      truncated: st.size > (end+1),
      query: q,
      regex: isRegex,
      icase,
      max: maxResults,
      results,
    });
  });
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'access-control-allow-origin': '*',
      'access-control-allow-headers': '*',
      'access-control-allow-methods': 'GET,OPTIONS',
    });
    return res.end();
  }

  try {
    const url = new URL(req.url, `http://localhost:${PORT}`);
    if (req.method !== 'GET') return writeJson(res, 405, { error: { message: 'Use GET' } });

    if (url.pathname === '/health') return writeJson(res, 200, { ok: true, roots: ROOTS });
    if (url.pathname === '/chunk') return handleChunk(req, res, url);
    if (url.pathname === '/lines') return handleLines(req, res, url);
    if (url.pathname === '/stat') return handleStat(req, res, url);

    return writeJson(res, 404, { error: { message: 'Not found' } });
  } catch (e) {
    return writeJson(res, 500, { error: { message: String(e?.message || e) } });
  }
});

server.listen(PORT, () => {
  console.log(`File Stream Server listening on http://localhost:${PORT}`);
  console.log(`Roots: ${ROOTS.join(', ')}`);
  console.log(`Max chunk: ${MAX_CHUNK} bytes, Max lines: ${MAX_LINES}`);
});
