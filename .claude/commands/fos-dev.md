# Start the Quorum development UI

Start the Flask dashboard for the Quorum project.

## Important: macOS port conflict

Port 5000 is occupied by AirPlay Receiver on macOS. Always use port **5050** (or whatever $ARGUMENTS specifies).

## Steps

1. Check if a server is already running on the target port:
   ```
   lsof -ti:5050
   ```
   If a process is found, ask the user whether to kill it or use a different port.

2. Start the Flask server in the background:
   ```
   PORT=5050 python3 fos_ui/app.py
   ```
   Run this with `run_in_background: true`.

3. Wait ~2 seconds, then verify it's up:
   ```
   curl -s http://localhost:5050/api/state | python3 -m json.tool
   ```
   Check that `"ok": true` is in the response.

4. Report:
   - URL: http://localhost:5050
   - Mock mode or live chain (based on whether `configured` is true in the state response)
   - How many proposals are active and whether any need action (`executable_now > 0`)

If `$ARGUMENTS` contains a port number, use that instead of 5050.

## What the UI shows

- **Registry panel** — all members with role, status, vote weight
- **Proposals panel** — quorum progress bars, vote chips, action buttons (Vote Yes/No, Execute, Release Funds)
- **Treasury panel** — ADA balance and per-proposal cap
- **Audit log** — recent operator agent decisions from `.fos_audit.jsonl`

Auto-refreshes every 30 seconds.
