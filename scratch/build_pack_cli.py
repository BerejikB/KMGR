import os, sys, json
from pathlib import Path

# Ensure KMGR root is set
os.environ.setdefault('KMGR_ROOT', r'K:\GOOSE\KMGR')

# Make import work when run from anywhere
sys.path.insert(0, r'K:\GOOSE\KMGR')

import server  # imports KMGR implementation and creates `kmgr` instance


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        max_mb = int(sys.argv[2]) if len(sys.argv) > 2 else 2048
    except Exception:
        max_mb = 2048
    res = server.kmgr.build_pack(repo=repo, max_pack_mb=max_mb)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
