# KMGR Debug State Pin (Do not expand in chat)

Protocol
- WSL-first execution; fallback to PowerShell only if WSL fails
- On any failure: attempt exactly one recovery, then halt and report
- After each block: `Success:<outcome>. Next:<plan>` or `Fail:<error>. Recover:<action>`
- Context handling: use kmgr_build_pack + kmgr_export_context for >50KB; read via kmgr_read_file_chunk; stream large files via ControlBridge filestream; never paste >2000 chars

Key paths
- Wrapper: K:\GOOSE\KMGR\kmgr_wrapper.cmd
- Server:  K:\GOOSE\KMGR\server.py
- Filestream: K:\GOOSE\KMGR\filestream\start_fs.ps1
- Logs: K:\GOOSE\KMGR\scratch\kmgr_err.log, kmgr_boot.txt, filestream_boot.err, filestream.log, filestream.err
- Extension name: kmgr

Status
- Enabling kmgr via platform__manage_extensions fails: "Channel closed" during stdio init

Planned next steps
1) Run server.py inside wrapper venv with verbose stdout/stderr to validate MCP stdio handshake and surface early exceptions
2) Verify Python interpreter resolved by kmgr_wrapper.cmd
3) Confirm filestream starts cleanly and does not block MCP init
