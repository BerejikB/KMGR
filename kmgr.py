# kmgr.py
from __future__ import annotations
import json, io, os, re, tempfile, time, hashlib, zipfile
from pathlib import Path

UTF8 = "utf-8"
NUL = b"\x00"

class KMGR:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.packs = self.root / "packs"
        self.scratch = self.root / "scratch"
        self.registry = self.root / "repos.json"
        self.packs.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        if not self.registry.exists():
            self.registry.write_text(json.dumps({"aliases": {}}, ensure_ascii=False), encoding=UTF8)

    # ---------- registry ----------
    def set_repo_alias(self, alias: str, path: str, default: bool=False):
        _assert(bool(alias) and re.match(r"^[A-Za-z0-9._-]{1,64}$", alias), "Invalid alias")
        p = Path(path).resolve(strict=True)
        _assert(p.is_dir(), f"Path not dir: {p}")
        reg = _read_json(self.registry)
        reg.setdefault("aliases", {})[alias] = str(p)
        if default:
            reg["default"] = alias
        _write_json_atomic(self.registry, reg)
        return {"alias": alias, "path": str(p), "default": reg.get("default")==alias}

    def _resolve_repo(self, repo: str|None) -> tuple[str, Path]:
        reg = _read_json(self.registry)
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
            a = reg["default"]; return a, Path(reg["aliases"][a])
        raise ValueError("No repo resolved: pass repo path/alias, set GOOSE_REPO, or set default alias")

    def _pack_path(self, alias: str) -> Path:
        return self.packs / f"{alias}_{time.strftime('%Y-%m-%d')}.kpkg"

    # ---------- build ----------
    def build_pack(self, repo: str|None=None, include: list[str]|None=None,
                   exclude_dirs: list[str]|None=None, max_pack_mb: int=2048) -> dict:
        alias, root = self._resolve_repo(repo)
        inc = include or ['*.md','*.txt','*.rst','*.py','*.ps1','*.psm1','*.cs','*.cpp','*.h','*.js','*.ts','*.tsx','*.json','*.yaml','*.yml','*.ini','*.toml','*.cfg','*.sql','*.sh','*.bat']
        exd = set(map(str.lower, exclude_dirs or ['.git','.venv','node_modules','dist','build','.idea','.vscode','.vs','__pycache__']))
        idx_rows = []
        ofs = 0
        enc = UTF8

        blob_tmp = Path(tempfile.mkstemp(prefix="kmgr_blob_", suffix=".bin")[1])
        try:
            with blob_tmp.open("wb") as bw:
                for p in root.rglob("*"):
                    if not p.is_file(): continue
                    if p.parent.name.lower() in exd: continue
                    if not _anymatch(p.name, inc): continue
                    try:
                        text = p.read_text(encoding=enc, errors="ignore").replace("\r\n","\n")
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
            with zipfile.ZipFile(pack, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
                _writestr(z, "meta.json", json.dumps(meta, ensure_ascii=False, separators=(",",":")))
                _writestr(z, "repo_index.csv", "".join(idx_rows))
                z.write(blob_tmp, "repo_content.bin")
                _writestr(z, "chat.jsonl", "")  # create if missing

            size = pack.stat().st_size
            _assert(size <= max_pack_mb * 1024 * 1024, f"Pack exceeds MaxPackMB ({size} bytes)")
            return {"pack": str(pack), "alias": alias, "repo": str(root), "size_bytes": size}
        finally:
            try: blob_tmp.unlink(missing_ok=True)
            except Exception: pass

    # ---------- append chat ----------
    def append_chat(self, role: str, content: str, repo: str|None=None, dedup: bool=True) -> dict:
        _assert(role in {"system","user","assistant","tool"}, "Invalid role")
        _assert(content and len(content) <= 2*1024*1024, "Empty/oversize content")
        alias, _ = self._resolve_repo(repo)
        pack = self._pack_path(alias)
        if not pack.exists():
            self.build_pack(repo)
        tmp = Path(tempfile.mkstemp(prefix="kmgr_chat_", suffix=".jsonl")[1])
        try:
            with zipfile.ZipFile(pack, "r") as z:
                try:
                    with z.open("chat.jsonl") as src, tmp.open("wb") as dst:
                        dst.write(src.read())
                except KeyError:
                    tmp.write_text("", encoding=UTF8)

            line = json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "role": role,
                "content": content,
                "cksum": hashlib.sha1(content.encode(UTF8)).hexdigest()
            }, ensure_ascii=False, separators=(",",":"))
            if dedup:
                if tmp.read_text(encoding=UTF8, errors="ignore").find(line[ line.rfind('"cksum":"')+9 : -2 ]) >= 0:
                    return {"pack": str(pack), "delta_bytes": 0}

            with tmp.open("a", encoding=UTF8) as f:
                f.write(line + "\n")

            _zip_replace(pack, "chat.jsonl", tmp)
            return {"pack": str(pack), "delta_bytes": len(line)+1}
        finally:
            try: tmp.unlink(missing_ok=True)
            except Exception: pass

    # ---------- export context ----------
    def export_context(self, query: str, repo: str|None=None, max_bytes: int=120_000, out_file: str|None=None) -> dict:
        _assert(query and isinstance(query, str), "Query required")
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

        # search chat (substring, case-insensitive)
        for ln in chat:
            if not ln: continue
            if q in ln.lower():
                hits.append({"type":"chat", "data": ln})

        # search repo by slicing bytes per index
        for row in idx:
            if not row: continue
            pth, start, length, _sha = row.split("\t", 3)
            start, length = int(start), int(length)
            s = blob[start:start+length].decode(UTF8, "ignore")
            if q in s.lower():
                preview = s[:2000]
                hits.append({"type":"repo", "path": pth, "start": start, "length": length, "preview": preview})

        enc = UTF8
        buf = io.StringIO()
        total = 0
        for h in hits:
            js = json.dumps(h, ensure_ascii=False, separators=(",",":"))
            need = len(js.encode(enc)) + 1
            if total + need > max_bytes: break
            buf.write(js + "\n")
            total += need

        _assert(total > 0, "Export produced empty context (broaden query or rebuild pack)")

        out = Path(out_file) if out_file else (self.scratch / "context_payload.txt")
        tmp = Path(tempfile.mkstemp(prefix="kmgr_ctx_", suffix=".txt")[1])
        try:
            tmp.write_text(buf.getvalue(), encoding=UTF8)
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(out)
        finally:
            try: tmp.unlink(missing_ok=True)
            except Exception: pass

        return {"pack": str(pack), "out_file": str(out), "bytes": total}

# ---- helpers ----
def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding=UTF8))
    except Exception:
        return {"aliases": {}}

def _write_json_atomic(path: Path, obj: dict):
    tmp = Path(tempfile.mkstemp(prefix="kmgr_reg_", suffix=".json")[1])
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding=UTF8)
        tmp.replace(path)
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass

def _writestr(z: zipfile.ZipFile, arc: str, s: str):
    z.writestr(arc, s.encode(UTF8))

def _zip_replace(pack: Path, arcname: str, src_file: Path):
    # replace single member atomically
    tmp_pack = Path(tempfile.mkstemp(prefix="kmgr_pack_", suffix=".kpkg")[1])
    with zipfile.ZipFile(pack, "r") as zin, zipfile.ZipFile(tmp_pack, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        done = False
        for it in zin.infolist():
            if it.filename == arcname:
                zout.write(src_file, arcname); done = True
            else:
                zout.writestr(it, zin.read(it))
        if not done:
            zout.write(src_file, arcname)
    tmp_pack.replace(pack)

def _anymatch(name: str, patterns: list[str]) -> bool:
    import fnmatch
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    return False

def _assert(cond: bool, msg: str):
    if not cond:
        raise ValueError(msg)
