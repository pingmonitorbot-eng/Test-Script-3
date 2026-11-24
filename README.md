# TempNumbers worker

Public GA worker → Mongo `tempnumbers_inbox` (`status=pending`).

## Secrets

| Secret | Required |
|--------|----------|
| `PANEL_USER` | yes |
| `PANEL_PASS` | yes |
| `MONGO_URI` | yes (same as big bot) |
| `MONGO_DB` | optional (default `tech_og_bot`) |
| `NEXT_RUN_TOKEN` | optional PAT for handover |

Never commit credentials.
