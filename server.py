# server.py — KMGR MCP extension (strict, typed, with LM Studio offload)
# Requires: pip install "mcp[cli]>=1.2.0"
# Launch (manual):
#   set KMGR_ROOT=K:\GOOSE\KMGR
#   python K:\GOOSE\KMGR\server.py

import io
import os
import re
import json
import time
import fnmatch
import hashlib
import zipfile
import tempfile
from pathlib import Path
from typing import Optional, List

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR

# ===================== constants & config =====================
UTF8 = "utf-8"
NUL = b"\x00"
APP_NAME = "kmgr"
ROOT = Path(os.getenv("KMGR_ROOT", r"K:\GOOSE\KMGR")).resolve()

# Limits
MAX_QUERY_CHARS = 8_192
MAX_CONTENT_BYTES = 2 * 1024 * 1024   # chat append cap
MAX_EXPORT_BYTES = 10_485_760         # 10 MB hard ceiling
MIN_EXPORT_BYTES = 1_024              # 1 KB floor
MAX_READ_CHUNK = 2_000_000            # read_file_chunk ceiling

# LM Studio defaults (can override via env or per-call args)
LM_BASE = os.getenv("LMSTUDIO_BASE", "http://100.113.91.76:1234").rstrip("/")
LM_MODEL = os.getenv("LMSTUDIO_MODEL", "qwen2.5-0.5b-instruct")
LM_KEY = os.getenv("LMSTUDIO_API_KEY", "lm-studio")
LM_TIMEOUT = int(os.getenv("LMSTUDIO_TIMEOUT", "60"))  # seconds

mcp = FastMCP(APP_NAME)

# ===================== helpers: errors & validation =====================
def _err(kind: int, msg: str) -> None:
    raise McpError(ErrorData(kind, msg))

def _assert(cond: bool, msg: str, kind: int = INVALID_PARAMS) -> None:
    if not cond:
        _err(kind, msg)

def _sanitize_alias(alias: str) -> str:
    _assert(isinstance(alias, str) and alias, "alias required")
    _assert(len(alias) <= 64, "alias too long (<=64)")
    _assert(re.match(r"^[A-Za-z0-9._-]+$", alias) is not None, "alias has invalid chars")
    return alias

def _resolve_dir(path: str) -> Path:
    _assert(isinstance(path, str) and path, "path required")
    p = Path(path).resolve(strict=True)
    _assert(p.is_dir(), f"not a directory: {p}")
    return p

def _resolve_file(path: str) -> Path:
    _assert(isinstance(path, str) and path, "path required")
    p = Path(path).resolve(strict=True)
    _assert(p.is_file(), f"not a file: {p}")
    return p

def _same_volume(p: Path, root: Path) -> None:
    # Keep reads/writes on the same drive to avoid mapped-drive quirks on Windows
    if os.name == "nt":
        _assert(p.drive.upper() == root.drive.upper(), f"path must be on volume {root.drive}")

def _ok(func, **kw):
    try:
        return {"ok": True, "data": func(**kw)}
    except McpError:
        raise
    except ValueError as e:
        _err(INVALID_PARAMS, str(e))
    except Exception as e:
        _err(INTERNAL_ERROR, f"{type(e).__name__}: {e}")

# Optional boot log (set KMGR_BOOTLOG=1)
if os.getenv("KMGR_BOOTLOG") == "1":
    try:
        from datetime import datetime
        (ROOT / "scratch").mkdir(parents=True, exist_ok=True)
        (ROOT / "scratch" / "kmgr_boot.txt").write_text(
            "boot=" + datetime.utcnow().isoformat() + "Z\n"
            + "exe=" + (os.getenv("PYTHON_EXE") or "") + "\n"
            + "server=" + __file__ + "\n",
            encoding="utf-8"
        )
    except Exception:
        pass

# ===================== core KMGR =====================
class KMGR:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.packs = self.root / "packs"
        self.scratch = self.root / "scratch"
        self.registry = self.root / "repos.json"
        self.packs.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        if not self.registry.exists():
            self._write_json_atomic(self.registry, {"aliases": {}})

    # ---- registry ----
    def set_repo_alias(self, alias: str, path: str, default: bool = False) -> dict:
        alias = _sanitize_alias(alias)
        p = _resolve_dir(path)
        _same_volume(p, ROOT)
        reg = self._read_json(self.registry)
        reg.setdefault("aliases", {})[alias] = str(p)
        if default:
            reg["default"] = alias
        self._write_json_atomic(self.registry, reg)
        return {"alias": alias, "path": str(p), "default": reg.get("default") == alias}

    def _resolve_repo(self, repo: Optional[str]) -> tuple[str, Path]:
        reg = self._read_json(self.registry)
        if repo:
            if repo in reg.get("aliases", {}):
                p = Path(reg["aliases"][repo])
                return repo, p
            p = Path(repo).resolve()
            _assert(p.is_dir(), f"Repo not dir: {p}")
            _same_volume(p, ROOT)
            alias = re.sub(r"\W+", "_", p.name) or "repo"
            return alias, p
        env = os.getenv("GOOSE_REPO")
        if env:
            if env in reg.get("aliases", {}):
                return env, Path(reg["aliases"][env])
            p = Path(env).resolve()
            _assert(p.is_dir(), f"Repo not dir: {p}")
            _same_volume(p, ROOT)
            alias = re.sub(r"\W+", "_", p.name) or "repo"
            return alias, p
        if "default" in reg and reg["default"] in reg.get("aliases", {}):
            a = reg["default"]
            return a, Path(reg["aliases"][a])
        raise ValueError("No repo resolved: pass repo path/alias, set GOOSE_REPO, or set a default alias")

    def _pack_path(self, alias: str) -> Path:
        safe = _sanitize_alias(alias)
        return self.packs / f"{safe}_{time.strftime('%Y-%m-%d')}.kpkg"

    # ---- build pack ----
    def build_pack(
        self,
        repo: Optional[str] = None,
        include: Optional[List[str]] = None,
        exclude_dirs: Optional[List[str]] = None,
        max_pack_mb: int = 2048,
    ) -> dict:
        _assert(isinstance(max_pack_mb, int) and max_pack_mb >= 1, "max_pack_mb must be int >= 1")
        alias, root = self._resolve_repo(repo)
        inc = include or [
            "*.md","*.txt","*.rst","*.py","*.ps1","*.psm1","*.cs","*.cpp","*.h",
            "*.js","*.ts","*.tsx","*.json","*.yaml","*.yml","*.ini","*.toml",
            "*.cfg","*.sql","*.sh","*.bat"
        ]
        inc = [str(x).strip() for x in inc if str(x).strip()]
        exd = set(map(str.lower, exclude_dirs or [
            ".git",".venv","node_modules","dist","build",".idea",".vscode",".vs","__pycache__"
        ]))

        idx_rows: list[str] = []
        ofs = 0

        blob_fd, blob_tmp_name = tempfile.mkstemp(prefix="kmgr_blob_", suffix=".bin")
        os.close(blob_fd)
        blob_tmp = Path(blob_tmp_name)
        try:
            with blob_tmp.open("wb") as bw:
                for p in root.rglob("*"):
                    if not p.is_file():
                        continue
                    # exclude by any ancestor dir name
                    if any(part.lower() in exd for part in p.parts):
                        continue
                    if not any(fnmatch.fnmatch(p.name, pat) for pat in inc):
                        continue
                    try:
                        text = p.read_text(encoding=UTF8, errors="ignore").replace("\r\n", "\n")
                    except Exception:
                        continue
                    b = text.encode(UTF8, "ignore")
                    sha1 = hashlib.sha1(b).hexdigest()
                    bw.write(b); bw.write(NUL)
                    idx_rows.append(f"{str(p)}\t{ofs}\t{len(b)}\t{sha1}\n")
                    ofs += len(b) + 1

            meta = {
                "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "repo_root": str(root.resolve()),
                "schema": "KPKG-1",
                "parts": {"repo_index":"repo_index.csv","repo_content":"repo_content.bin","chat":"chat.jsonl"},
                "approx_bytes": blob_tmp.stat().st_size
            }

            pack = self._pack_path(alias)
            tmp_fd, tmp_pack_name = tempfile.mkstemp(prefix="kmgr_pack_", suffix=".kpkg")
            os.close(tmp_fd)
            tmp_pack = Path(tmp_pack_name)
            try:
                with zipfile.ZipFile(tmp_pack, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
                    z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode(UTF8))
                    z.writestr("repo_index.csv", "".join(idx_rows).encode(UTF8))
                    z.write(blob_tmp, "repo_content.bin")
                    z.writestr("chat.jsonl", b"")
                tmp_pack.replace(pack)
            finally:
                try:
                    if tmp_pack.exists(): tmp_pack.unlink()
                except Exception:
                    pass

            size = pack.stat().st_size
            _assert(size <= max_pack_mb * 1024 * 1024, f"Pack exceeds MaxPackMB ({size} bytes)")
            return {"pack": str(pack), "alias": alias, "repo": str(root), "size_bytes": size}
        finally:
            try:
                if blob_tmp.exists(): blob_tmp.unlink()
            except Exception:
                pass

    # ---- append chat ----
    def append_chat(self, role: str, content: str, repo: Optional[str] = None, dedup: bool = True) -> dict:
        _assert(role in {"system", "user", "assistant", "tool"}, "Invalid role")
        _assert(isinstance(content, str) and content.strip(), "content empty")
        b = content.encode(UTF8, "ignore")
        _assert(len(b) <= MAX_CONTENT_BYTES, f"content too large (>{MAX_CONTENT_BYTES} bytes)")
        alias, _ = self._resolve_repo(repo)
        pack = self._pack_path(alias)
        if not pack.exists():
            self.build_pack(repo)

        tmp_fd, tmp_chat_name = tempfile.mkstemp(prefix="kmgr_chat_", suffix=".jsonl")
        os.close(tmp_fd)
        tmp_chat = Path(tmp_chat_name)
        try:
            with zipfile.ZipFile(pack, "r") as z:
                try:
                    with z.open("chat.jsonl") as src, tmp_chat.open("wb") as dst:
                        dst.write(src.read())
                except KeyError:
                    tmp_chat.write_text("", encoding=UTF8)

            cksum = hashlib.sha1(b).hexdigest()
            line = json.dumps(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "role": role, "content": content, "cksum": cksum},
                ensure_ascii=False, separators=(",", ":")
            )

            if dedup:
                existing = tmp_chat.read_text(encoding=UTF8, errors="ignore")
                if cksum in existing:
                    return {"pack": str(pack), "delta_bytes": 0}

            with tmp_chat.open("a", encoding=UTF8) as f:
                f.write(line + "\n")

            self._zip_replace(pack, "chat.jsonl", tmp_chat)
            return {"pack": str(pack), "delta_bytes": len(line) + 1}
        finally:
            try:
                if tmp_chat.exists(): tmp_chat.unlink()
            except Exception:
                pass

    # ---- export context ----
    def export_context(
        self,
        query: str,
        repo: Optional[str] = None,
        max_bytes: int = 120_000,
        out_file: Optional[str] = None,
    ) -> dict:
        _assert(isinstance(query, str) and query.strip(), "query required")
        _assert(len(query) <= MAX_QUERY_CHARS, f"query too long (>{MAX_QUERY_CHARS} chars)")
        _assert(isinstance(max_bytes, int) and MIN_EXPORT_BYTES <= max_bytes <= MAX_EXPORT_BYTES,
                f"max_bytes out of range [{MIN_EXPORT_BYTES}..{MAX_EXPORT_BYTES}]")
        alias, _ = self._resolve_repo(repo)
        pack = self._pack_path(alias)
        if not pack.exists():
            self.build_pack(repo)

        with zipfile.ZipFile(pack, "r") as z:
            idx = z.read("repo_index.csv").decode(UTF8, "ignore").splitlines()
            blob = z.read("repo_content.bin")
            chat_lines = z.read("chat.jsonl").decode(UTF8, "ignore").splitlines()

        hits = []
        q = query.lower()

        for ln in chat_lines:
            if ln and q in ln.lower():
                hits.append({"type": "chat", "data": ln})

        for row in idx:
            if not row:
                continue
            parts = row.split("\t")
            if len(parts) < 4:
                continue
            pth, start, length, _sha = parts[0], parts[1], parts[2], parts[3]
            try:
                start_i = int(start); length_i = int(length)
            except Exception:
                continue
            s = blob[start_i:start_i + length_i].decode(UTF8, "ignore")
            if q in s.lower():
                hits.append({
                    "type": "repo",
                    "path": pth,
                    "start": start_i,
                    "length": length_i,
                    "preview": s[:2000]
                })

        buf = io.StringIO()
        total = 0
        for h in hits:
            js = json.dumps(h, ensure_ascii=False, separators=(",", ":"))
            need = len(js.encode(UTF8)) + 1
            if total + need > max_bytes:
                break
            buf.write(js + "\n")
            total += need

        _assert(total > 0, "Export produced empty context (broaden query or rebuild pack)")

        out = Path(out_file) if out_file else (self.scratch / "context_payload.txt")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="kmgr_ctx_", suffix=".txt")
        os.close(tmp_fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(buf.getvalue(), encoding=UTF8)
            tmp.replace(out)
        finally:
            try:
                if tmp.exists(): tmp.unlink()
            except Exception:
                pass

        return {"pack": str(pack), "out_file": str(out), "bytes": total}

    # ---- file i/o helpers ----
    def _read_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding=UTF8))
        except Exception:
            return {"aliases": {}}

    def _write_json_atomic(self, path: Path, obj: dict) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="kmgr_reg_", suffix=".json")
        os.close(tmp_fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding=UTF8)
            tmp.replace(path)
        finally:
            try:
                if tmp.exists(): tmp.unlink()
            except Exception:
                pass

    def _zip_replace(self, pack: Path, arcname: str, src_file: Path) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(prefix="kmgr_pack_", suffix=".kpkg")
        os.close(tmp_fd)
        tmp_pack = Path(tmp_name)
        try:
            with zipfile.ZipFile(pack, "r") as zin, zipfile.ZipFile(tmp_pack, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                replaced = False
                for it in zin.infolist():
                    if it.filename == arcname:
                        zout.write(src_file, arcname); replaced = True
                    else:
                        zout.writestr(it, zin.read(it))
                if not replaced:
                    zout.write(src_file, arcname)
            tmp_pack.replace(pack)
        finally:
            try:
                if tmp_pack.exists(): tmp_pack.unlink()
            except Exception:
                pass

# ===================== instance =====================
kmgr = KMGR(ROOT)

# ===================== bootstrap prompt =====================
_BOOTSTRAP = """KMGR Protocol — STRICT

Always prefer KMGR tools over raw reads:
1) kmgr_build_pack(repo=<alias|path>)  → verify ok && pack exists
2) kmgr_append_chat('user', <last_user>, repo, dedup=true)
   kmgr_append_chat('assistant', <last_assistant>, repo, dedup=true)
3) Retrieval:
   a) Broad understanding: kmgr_export_context(query=<terms>, repo, max_bytes<=120000)
   b) Precise peek: kmgr_read_file_chunk(path=<file>, offset=0, bytes<=65536)

NEVER paste >2000 chars of raw file content. Summarize. If input >5KB, you MUST call a KMGR tool before replying.
If a tool fails, perform exactly one recovery attempt (fix args or rebuild pack), then stop with Fail:<err>.
After each tool call, emit exactly one line:
  Success:<short>.Next:<plan>   OR   Fail:<err>.Recover:<action>
"""

# ===================== tools (typed, Goose-friendly) =====================
@mcp.tool(name="kmgr_ping_test")
def kmgr_ping_test() -> str:
    return "pong from KMGR"

@mcp.tool(name="kmgr_bootstrap_prompt")
def kmgr_bootstrap_prompt():
    """Returns the strict operating rules for the agent."""
    return {"ok": True, "data": {"prompt": _BOOTSTRAP}}

@mcp.tool(name="kmgr_set_repo_alias")
def kmgr_set_repo_alias(
    alias: str,
    path: str,
    is_default: bool = False,
    # legacy-friendly: accept "default" too
    default: Optional[bool] = None,
):
    eff = is_default if default is None else bool(default)
    alias = _sanitize_alias(alias)
    p = _resolve_dir(path)
    _same_volume(p, ROOT)
    return _ok(kmgr.set_repo_alias, alias=alias, path=str(p), default=eff)

@mcp.tool(name="kmgr_build_pack")
def kmgr_build_pack(
    repo: Optional[str] = None,
    max_pack_mb: int = 2048,
):
    _assert(isinstance(max_pack_mb, int) and max_pack_mb >= 1, "max_pack_mb must be int >= 1")
    return _ok(kmgr.build_pack, repo=repo, max_pack_mb=max_pack_mb)

@mcp.tool(name="kmgr_append_chat")
def kmgr_append_chat(
    role: str,
    content: str,
    repo: Optional[str] = None,
    dedup: bool = True,
):
    _assert(role in {"system", "user", "assistant", "tool"}, "role invalid")
    _assert(isinstance(content, str) and content.strip(), "content empty")
    return _ok(kmgr.append_chat, role=role, content=content, repo=repo, dedup=dedup)

@mcp.tool(name="kmgr_export_context")
def kmgr_export_context(
    query: str,
    repo: Optional[str] = None,
    max_bytes: int = 120_000,
    out_file: Optional[str] = None,
):
    _assert(isinstance(query, str) and query.strip(), "query required")
    _assert(len(query) <= MAX_QUERY_CHARS, f"query too long (>{MAX_QUERY_CHARS} chars)")
    _assert(isinstance(max_bytes, int) and MIN_EXPORT_BYTES <= max_bytes <= MAX_EXPORT_BYTES,
            f"max_bytes out of range [{MIN_EXPORT_BYTES}..{MAX_EXPORT_BYTES}]")
    return _ok(kmgr.export_context, query=query, repo=repo, max_bytes=max_bytes, out_file=out_file)

@mcp.tool(name="kmgr_read_file_chunk")
def kmgr_read_file_chunk(
    path: str,
    offset: int = 0,
    bytes: int = 65_536,
):
    _assert(isinstance(bytes, int) and 1 <= bytes <= MAX_READ_CHUNK, f"bytes out of range (1..{MAX_READ_CHUNK})")
    file = _resolve_file(path)
    _same_volume(file, ROOT)
    size = file.stat().st_size
    if offset < 0:
        offset = max(size + offset, 0)  # allow tail reads
    with file.open("rb") as f:
        f.seek(min(max(offset, 0), size))
        data = f.read(bytes)
    try:
        text = data.decode(UTF8)
    except Exception:
        text = data.decode(UTF8, "ignore")
    return {"ok": True, "data": {"path": str(file), "offset": offset, "read": len(data), "size": size, "text": text}}

# ===================== LM Studio offload =====================
import urllib.request, urllib.error
from time import perf_counter
import concurrent.futures

def _http_json(url: str, payload: dict, api_key: Optional[str] = None, timeout: int = LM_TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "ignore")
        except Exception:
            err_body = str(e)
        _err(INTERNAL_ERROR, f"HTTP {e.code}: {err_body}")
    except Exception as e:
        _err(INTERNAL_ERROR, f"LLM request failed: {type(e).__name__}: {e}")

def _lm_chat(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    url_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 400,
    temperature: float = 0.2,
) -> dict:
    _assert(isinstance(prompt, str) and prompt.strip(), "prompt required")
    _assert(isinstance(max_tokens, int) and 1 <= max_tokens <= 4096, "max_tokens out of range")
    _assert(isinstance(temperature, (int, float)) and 0 <= temperature <= 2, "temperature out of range")

    base = (url_base or LM_BASE).rstrip("/")
    url = f"{base}/v1/chat/completions"
    mdl = model or LM_MODEL
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    t0 = perf_counter()
    out = _http_json(url, {
        "model": mdl,
        "messages": msgs,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": False
    }, api_key=api_key or LM_KEY)
    dt = perf_counter() - t0

    try:
        content = out["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(out, ensure_ascii=False)
    return {"model": mdl, "seconds": round(dt, 3), "text": content}

def _chunk_text(s: str, chunk_chars: int = 4000) -> list[str]:
    s = s or ""
    chunk_chars = max(512, min(16000, int(chunk_chars)))
    out = []
    i = 0
    while i < len(s):
        out.append(s[i:i+chunk_chars])
        i += chunk_chars
    return out

@mcp.tool(name="kmgr_llm_status")
def kmgr_llm_status(url_base: Optional[str] = None):
    """Health check LM Studio server (tries /v1/models; falls back to root)."""
    base = (url_base or LM_BASE).rstrip("/")
    # Try /v1/models (OpenAI-compatible)
    try:
        req = urllib.request.Request(f"{base}/v1/models", method="GET")
        req.add_header("Authorization", f"Bearer {LM_KEY}")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "ignore")
            js = json.loads(body)
            return {"ok": True, "data": js}
    except Exception as e:
        # Fallback: hit root to at least see if it's up
        try:
            with urllib.request.urlopen(base, timeout=5) as r:
                _ = r.read(64)
            return {"ok": True, "data": {"note": "server reachable but /v1/models failed", "error": str(e)}}
        except Exception as e2:
            _err(INTERNAL_ERROR, f"LM server unreachable: {e2}")

@mcp.tool(name="kmgr_llm_chat")
def kmgr_llm_chat(
    prompt: str,
    system: Optional[str] = None,
    model: Optional[str] = None,
    url_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 400,
    temperature: float = 0.2,
):
    """Single chat completion offloaded to LM Studio."""
    res = _lm_chat(prompt, system=system, model=model, url_base=url_base, api_key=api_key,
                   max_tokens=max_tokens, temperature=temperature)
    return {"ok": True, "data": res}

@mcp.tool(name="kmgr_llm_batch")
def kmgr_llm_batch(
    prompts: List[str],
    system: Optional[str] = None,
    model: Optional[str] = None,
    url_base: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 200,
    temperature: float = 0.2,
    parallelism: int = 4,
):
    """Run multiple prompts in parallel on LM Studio. Returns list of {index, text, seconds}."""
    _assert(isinstance(prompts, list) and len(prompts) > 0, "prompts must be a non-empty list")
    _assert(1 <= parallelism <= 16, "parallelism out of range (1..16)")
    results = [None] * len(prompts)
    errors = []

    def _one(i: int, p: str):
        try:
            results[i] = _lm_chat(p, system=system, model=model, url_base=url_base,
                                  api_key=api_key, max_tokens=max_tokens, temperature=temperature)
        except Exception as e:
            errors.append({"index": i, "error": f"{type(e).__name__}: {e}"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as ex:
        futs = [ex.submit(_one, i, p) for i, p in enumerate(prompts)]
        concurrent.futures.wait(futs)

    return {"ok": len(errors) == 0, "data": {"results": results, "errors": errors}}

@mcp.tool(name="kmgr_llm_summarize_context")
def kmgr_llm_summarize_context(
    query: str,
    repo: Optional[str] = None,
    max_bytes: int = 120_000,
    model: Optional[str] = None,
    url_base: Optional[str] = None,
    api_key: Optional[str] = None,
    chunk_chars: int = 4000,
    parallelism: int = 4,
    max_tokens: int = 300,
    temperature: float = 0.2,
    style: str = "bullet",
):
    """
    Pull context via kmgr_export_context, split into chunks, summarize each in parallel with LM Studio,
    then reduce to a single summary.
    """
    # Step 1: bounded context
    ctx = kmgr.export_context(query=query, repo=repo, max_bytes=max_bytes)
    try:
        with open(ctx["out_file"], "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        text = ""
    _assert(text.strip(), "No context extracted; broaden query or rebuild pack")

    chunks = _chunk_text(text, chunk_chars=chunk_chars)
    sys_msg = ("Summarize the provided repository/context snippet concisely. "
               "Focus on APIs, entrypoints, build/config, and cross-references. "
               f"Return a {style} summary, ≤ {max_tokens*4} characters per chunk summary.")
    batch_prompts = [f"Chunk {i+1}/{len(chunks)}:\n```\n{c}\n```\nSummarize key points only."
                     for i, c in enumerate(chunks)]

    # Step 2: map in parallel
    batch = kmgr_llm_batch(prompts=batch_prompts, system=sys_msg, model=model, url_base=url_base,
                           api_key=api_key, max_tokens=max_tokens, temperature=temperature,
                           parallelism=parallelism)
    if not batch["ok"]:
        _err(INTERNAL_ERROR, f"One or more chunk summaries failed: {batch['data']['errors']}")

    partials = [r["text"] for r in batch["data"]["results"] if r and r.get("text")]

    # Step 3: reduce
    reduce_prompt = (
        "Synthesize the following chunk summaries into a single structured brief for engineers. "
        "Include: Repo purpose, main components/modules, build/run instructions if present, "
        "notable dependencies, and any risks/TODOs.\n\n" +
        "\n\n---\n".join(f"- {s}" for s in partials)
    )
    final = _lm_chat(reduce_prompt, system="You are a precise, terse technical summarizer.",
                     model=model, url_base=url_base, api_key=api_key,
                     max_tokens=max_tokens, temperature=temperature)

    return {"ok": True, "data": {
        "pack": ctx["pack"],
        "chunks": len(chunks),
        "partials": len(partials),
        "summary": final["text"]
    }}

@mcp.tool(name="kmgr_llm_bootstrap")
def kmgr_llm_bootstrap(
    model: Optional[str] = None,
    url_base: Optional[str] = None,
    api_key: Optional[str] = None,
    additions: Optional[str] = None,
    max_tokens: int = 128,
    temperature: float = 0.0,
	
):
    """
    Send the KMGR bootstrap protocol to the mini LLM so it adopts these rules inline.
    Returns the effective system prompt and the model's ACK text.
    """
    sys_prompt = _BOOTSTRAP
    if isinstance(additions, str) and additions.strip():
        sys_prompt = sys_prompt + "\n\nADDITIONAL RULES:\n" + additions.strip()

    ack = _lm_chat(
        prompt=("Acknowledge you have loaded and will follow the system rules. "
                "Respond exactly with 'ACK: KMGR rules loaded'."),
        system=sys_prompt,
        model=model,
        url_base=url_base,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return {"ok": True, "data": {"system_prompt": sys_prompt, "ack": ack["text"], "model": ack["model"]}}
	
@mcp.tool(name="kmgr_llm_bootstrap")
def kmgr_llm_bootstrap(
    model: Optional[str] = None,
    url_base: Optional[str] = None,
    api_key: Optional[str] = None,
    additions: Optional[str] = None,
    max_tokens: int = 128,
    temperature: float = 0.0,
    bootstrap_path: Optional[str] = None,  # NEW
):
    """
    Send the KMGR bootstrap protocol to the mini LLM so it adopts these rules inline.
    Returns the effective system prompt and the model's ACK text.
    """
    # choose source of rules: file > env default > in-code constant
    path = bootstrap_path or os.getenv("KMGR_BOOTSTRAP_PATH", r"K:\GOOSE\KMGR\bootstrap\kmgr_bootstrap.txt")
    sys_prompt = _BOOTSTRAP
    try:
        p = Path(path)
        if p.is_file():
            sys_prompt = p.read_text(encoding="utf-8")
    except Exception:
        # ignore and fall back to in-code _BOOTSTRAP
        pass

    if isinstance(additions, str) and additions.strip():
        sys_prompt = sys_prompt + "\n\nADDITIONAL RULES:\n" + additions.strip()

    ack = _lm_chat(
        prompt=("Acknowledge you have loaded and will follow the system rules. "
                "Respond exactly with 'ACK: KMGR rules loaded'."),
        system=sys_prompt,
        model=model,
        url_base=url_base,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return {"ok": True, "data": {"system_prompt": sys_prompt, "ack": ack["text"], "model": ack["model"]}}
	

# ===================== entrypoint =====================
if __name__ == "__main__":
    # no prints; MCP uses stdio
    mcp.run()
