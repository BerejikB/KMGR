# server.py
import os, json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS, INTERNAL_ERROR
from kmgr import KMGR

ROOT = Path(os.getenv("KMGR_ROOT", r"K:\GOOSE\KMGR")).resolve()
kmgr = KMGR(ROOT)
mcp = FastMCP("kmgr")

def _ok(f, **kw):
    try:
        return {"ok": True, "data": f(**kw)}
    except ValueError as e:
        raise McpError(ErrorData(INVALID_PARAMS, str(e)))
    except Exception as e:
        raise McpError(ErrorData(INTERNAL_ERROR, f"{type(e).__name__}: {e}"))

@mcp.tool()
def set_repo_alias(alias: str, path: str, is_default: bool=False):
    if not alias or not path: raise McpError(ErrorData(INVALID_PARAMS, "alias and path required"))
    return _ok(kmgr.set_repo_alias, alias=alias, path=path, default=is_default)

@mcp.tool()
def build_pack(repo: str|None=None, max_pack_mb: int=2048):
    return _ok(kmgr.build_pack, repo=repo, max_pack_mb=max_pack_mb)

@mcp.tool()
def append_chat(role: str, content: str, repo: str|None=None, dedup: bool=True):
    if role not in {"system","user","assistant","tool"}:
        raise McpError(ErrorData(INVALID_PARAMS, "role invalid"))
    if not content:
        raise McpError(ErrorData(INVALID_PARAMS, "content empty"))
    return _ok(kmgr.append_chat, role=role, content=content, repo=repo, dedup=dedup)

@mcp.tool()
def export_context(query: str, repo: str|None=None, max_bytes: int=120000, out_file: str|None=None):
    if not query:
        raise McpError(ErrorData(INVALID_PARAMS, "query required"))
    return _ok(kmgr.export_context, query=query, repo=repo, max_bytes=max_bytes, out_file=out_file)

if __name__ == "__main__":
    mcp.run()
