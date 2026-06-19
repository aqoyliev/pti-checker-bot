# Incident Log

Operational incidents detected by the automated deploy-watchdog.

---

## 2026-06-17 — Service offline: all Railway deployments removed

**Detected:** 2026-06-19 (watchdog run)  
**Outage start:** 2026-06-17 ~09:27 UTC  
**Duration at detection:** ~2 days  
**Status:** Requires manual action to restore

### Symptom

`pti-checker-bot` service shows **Offline** in Railway. All 10 historical
deployments have status `REMOVED`. No active deployment is running.

### Evidence

`railway status`:
```
Services
  - pti-checker-bot: ○ Offline
  - Postgres:         ● Online
```

Railway GraphQL — last 10 deployments, all `REMOVED`:

| Created (UTC)       | Commit   | Status  |
|---------------------|----------|---------|
| 2026-06-16 13:22    | 9ea95c1  | REMOVED |
| 2026-06-16 02:00    | c0cb8be  | REMOVED |
| 2026-06-16 02:00    | bba88a7  | REMOVED |
| 2026-06-15 12:50    | 1d435f5  | REMOVED |
| 2026-06-15 09:42    | 0f6cb3d  | REMOVED |
| 2026-06-15 09:19    | d44fb91  | REMOVED |
| 2026-06-15 08:18    | 8a4b33e  | REMOVED |
| 2026-06-15 07:58    | 322988c  | REMOVED |
| 2026-06-15 07:23    | 6161f22  | REMOVED |
| 2026-06-13 06:51    | 72ed80f  | REMOVED |

Last log lines from the final container (`railway logs`):
```
models.py [LINE:6323] #INFO  [2026-06-17 09:17:03] AFC is enabled with max remote calls: 10.
pti_processor.py [LINE:68] #WARNING [2026-06-17 09:17:16] Gemini transient error on attempt 4 (ServerError); no more retries
pti_processor.py [LINE:500] #WARNING [2026-06-17 09:17:16] Gemini overloaded after retries (mixed-media flow): ServerError
Stopping Container
```

### Diagnosis

The Gemini `ServerError` on the last PTI run was handled gracefully by the
existing retry/backoff logic — it surfaced a friendly "overloaded" message to
the user and did not crash the process. The `Stopping Container` message
immediately following it is a Railway infrastructure event (not an application
crash). All deployments subsequently show `REMOVED` status with no replacement
deployment, indicating the service was stopped/removed through the Railway
dashboard (intentional or accidental). There is no code bug causing this.

### Action required

Re-deploy the service through the Railway dashboard:
1. Go to the Railway project → `pti-checker-bot` service.
2. Trigger a new deployment from the `master` branch (latest commit: `9ea95c1`).
   All 10 prior deployments report `canRedeploy: true`.
3. Verify the container starts (`railway logs`) and the bot responds in Telegram.

### Code state

Healthy. The current `master` tip (`9ea95c1`) was the last deployed commit.
No code changes are needed.
