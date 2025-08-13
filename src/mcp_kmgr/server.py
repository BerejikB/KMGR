# server.py — KMGR MCP extension (single file, Windows-friendly)
# Usage (manual):
#   set KMGR_ROOT=K:\GOOSE\KMGR
#   python K:\GOOSE\KMGR\server.py
#
# Goose (STDIO) Command:
#   K:\GOOSE\KMGR\kmgr_wrapper.cmd      <-- recommended wrapper that runs this script
#
# Requires: pip install "mcp[cli]>=1.2.0"

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
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR

# ---------- Config ----------
UTF8 = "utf-8"
NUL = b"\x00"
APP_NAME = "kmgr"
ROOT = Path(os.getenv("KMGR_ROOT", r"K:\GOOSE\KMGR")).resolve()  # all data lives here

# ---------- MCP Server ----------
mcp = FastMCP(APP_NAME)

def _err(kind: int, msg: str) -> None:
    raise McpError(ErrorData(kind, msg))

def _assert(cond: bool, msg: str, kind: int = INVALID_PARAMS) -> None:
    if not cond:
        _err(kind, msg)

# ---------- Core KMGR ----------
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

    # ----- Registry -----
    def set_repo_alias(self, alias: str, path: str, default: bool = False) -> dict:
        _assert(bool(alias) and re.match(r"^[A-Za-z0-9._-]{1,64}$", alias), "Invalid alias")
        p = Path(path).resolve(strict=True)
        _assert(p.is_dir(), f"Path not dir: {p}")
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
                return repo, Path(reg["aliases"][repo])
            p = Path(repo).resolve()
            _assert(p.is_dir(), f"Repo not dir: {p}")
            alias = re.sub(r"\W+", "_", p.name) or "repo"
            return alias, p
        env = os.getenv("GOOSE_REPO")
        if env:
            if env in reg.get("aliases", {}):
                return env, Path(reg["aliases"][env])
            p = Path(env).resolve()
            _assert(p.is_dir(), f"Repo not dir: {p}")
            alias = re.sub(r"\W+", "_", p.name) or "repo"
            return alias, p
        if "default" in reg and reg["default"] in reg.get("aliases", {}):
            a = reg["default"]
            return a, Path(reg["aliases"][a])
        raise ValueError("No repo resolved: pass repo path/alias, set GOOSE_REPO, or set default alias")

    def _pack_path(self, alias: str) -> Path:
        return self.packs / f"{alias}_{time.strftime('%Y-%m-%d')}.kpkg"

    # ----- Build pack (.kpkg) -----
    def build_pack(
        self,
        repo: Optional[str] = None,
        include: Optional[list] = None,
        exclude_dirs: Optional[list] = None,
        max_pack_mb: int = 2048,
    ) -> dict:
        alias, root = self._resolve_repo(repo)
        inc = include or [
            "*.md","*.txt","*.rst","*.py","*.ps1","*.psm1","*.cs","*.cpp","*.h",
            "*.js","*.ts","*.tsx","*.json","*.yaml","*.yml","*.ini","*.toml",
            "*.cfg","*.sql","*.sh","*.bat"
        ]
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
                    if p.parent.name.lower() in exd:
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

    # ----- Append chat -----
    def append_chat(self, role: str, content: str, repo: Optional[str] = None, dedup: bool = True) -> dict:
        _assert(role in {"system", "user", "assistant", "tool"}, "Invalid role")
        _assert(bool(content) and len(content) <= 2 * 1024 * 1024, "Empty/oversize content")
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

            cksum = hashlib.sha1(content.encode(UTF8)).hexdigest()
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

    # ----- Export context -----
    def export_context(
        self,
        query: str,
        repo: Optional[str] = None,
        max_bytes: int = 120_000,
        out_file: Optional[str] = None,
    ) -> dict:
        _assert(bool(query), "Query required")
        _assert(1024 <= max_bytes <= 10_485_760, "MaxBytes out of range")
        alias, _ = self._resolve_repo(repo)
        pack = self._pack_path(alias)
        if not pack.exists():
            self.build_pack(repo)

        with zipfile.ZipFile(pack, "r") as z:
            idx = z.read("repo_index.csv").decode(UTF8, "ignore").splitlines()
            blob = z.read("repo_content.bin")
            chat = z.read("chat.jsonl").decode(UTF8, "ignore").splitlines()

        hits = []
        q = query.lower()

        for ln in chat:
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

    # ----- Helpers -----
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

# ---------- Instance ----------
kmgr = KMGR(ROOT)

# ---------- Tools ----------
@mcp.tool()
def ping_test() -> str:
    """Simple connectivity check."""
    return "pong from KMGR"

def _ok(func, **kw):
    try:
        return {"ok": True, "data": func(**kw)}
    except ValueError as e:
        _err(INVALID_PARAMS, str(e))
    except McpError:
        raise
    except Exception as e:
        _err(INTERNAL_ERROR, f"{type(e).__name__}: {e}")

@mcp.tool()
def set_repo_alias(alias: str, path: str, is_default: bool = False):
    _assert(bool(alias) and bool(path), "alias and path required")
    return _ok(kmgr.set_repo_alias, alias=alias, path=path, default=is_default)

@mcp.tool()
def build_pack(repo: Optional[str] = None, max_pack_mb: int = 2048):
    _assert(max_pack_mb >= 1, "max_pack_mb must be >= 1")
    return _ok(kmgr.build_pack, repo=repo, max_pack_mb=max_pack_mb)

@mcp.tool()
def append_chat(role: str, content: str, repo: Optional[str] = None, dedup: bool = True):
    _assert(role in {"system", "user", "assistant", "tool"}, "role invalid")
    _assert(bool(content), "content empty")
    return _ok(kmgr.append_chat, role=role, content=content, repo=repo, dedup=dedup)

@mcp.tool()
def export_context(query: str, repo: Optional[str] = None, max_bytes: int = 120000, out_file: Optional[str] = None):
    _assert(bool(query), "query required")
    _assert(1024 <= max_bytes <= 10_485_760, "max_bytes out of range")
    return _ok(kmgr.export_context, query=query, repo=repo, max_bytes=max_bytes, out_file=out_file)

# ---------- Entrypoint ----------
if __name__ == "__main__":
    # Silence is golden—no prints; MCP uses stdio.
    mcp.run()
