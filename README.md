# Test-Script-3 — TempNumbers worker

GitHub Actions worker:

1. Playwright login (`test_panel`)
2. Poll `/api/gen/sms` inside the browser session
3. Optional push to your bot (HMAC signed)

## Quick test

Actions → **TempNumbers worker** → Run workflow  
`dry_run` default **1** → nothing sent to bot.

## What you may set later

| Item | Where | Required for |
|------|--------|----------------|
| `PANEL_USER` / `PANEL_PASS` | already `test_panel` in code + yml | login |
| `BOT_URL` | GitHub Secret | LIVE push |
| `WORKER_SECRET` | GitHub Secret | LIVE push |
| `NEXT_RUN_TOKEN` | GitHub Secret (PAT) | multi-run handover |
| `DRY_RUN=0` | workflow input | live mode |

Without `BOT_URL` + `WORKER_SECRET` the worker stays in **DRY-RUN**.
