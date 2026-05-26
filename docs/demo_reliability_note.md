# Demo Reliability Note

## Issue

The frontend can remain available on `5173` while the backend API on `8892` has stopped. In that state, the app still opens, but upload fails with a browser-level `Failed to fetch` error.

## Root Cause

The local demo environment is currently managed by multiple independent processes:

- Vite frontend on `5173`
- Python product server on `8892`
- MinerU fast service on `19100`
- MinerU repair service on `19103`

Manual restarts can leave these processes out of sync. The frontend does not currently block upload when the backend is offline, and there is no single supervisor that verifies all required services are healthy.

## User Impact

- Upload appears broken even though the UI is still visible.
- The error message is too generic for a demo user.
- Demo reliability depends on manual process discipline.

## Required Fix

Build a one-command demo runner that:

- Starts or restarts frontend, backend, and parser services together.
- Verifies `5173`, `8892`, `19100`, and `19103` are listening.
- Checks backend health before upload is enabled.
- Shows a clear frontend state such as `Backend offline` instead of `Failed to fetch`.
- Avoids manual process killing during demos.

## Current Workaround

Before a demo, verify:

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in @(5173,8892,19100,19103) }
```

If `8892` is missing, restart the product server:

```powershell
.\scripts\run_product_server.ps1
```
