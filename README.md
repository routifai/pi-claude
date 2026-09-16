# Milhe Harness Prototype

Multi-harness chat UI with Claude + Pi, with in-session harness switching.

## Setup

### 1. Install Omnigent

```bash
uv tool install omnigent
```

### 2. Start Omnigent server

```bash
omnigent server --background
# or verify it's running:
omnigent server status
```

### 3. Backend

```bash
cd backend
# Copy .env.example to .env and add your ANTHROPIC_API_KEY
# (or it's auto-filled from hypatia/.env if you ran setup)
pip install -r requirements.txt
uvicorn main:app --reload
```

Backend runs at `http://localhost:8000`.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend runs at `http://localhost:5173` (Vite's default).

Pi requires its own auth: `~/.pi/agent/auth.json` with `{"anthropic": {"type": "api_key", "key": "..."}}`.

## Test the Success Criterion

1. Open `http://localhost:5173`.
2. Click "New Session".
3. Type a message → real reply from Claude, tagged "claude".
4. Click the "Pi" button to switch harness.
5. Type another message → real reply from Pi, tagged "pi", **in the same visible thread**.
6. Switch back to Claude and ask it about something Pi said earlier — it knows, because the full transcript is passed on every turn, not just the latest message.
7. Toggle "Plan Mode" on and repeat a request — the active harness describes a plan instead of acting, for either harness.
8. Reload the page / click "New Session" → both messages still there with correct harness tags.

## Notes

- **Real Claude and Pi, both live.** `ClaudeSDKExecutor` and `PiExecutor` are used per their actual `harness` field — no shortcuts.
- **Session isolation.** Executors are keyed by `(session_id, harness, plan_mode)`, so concurrent prototype sessions never share conversation state, and toggling plan mode gets a fresh executor with the right `permission_mode` for Claude.
- **Cross-harness history.** Every call passes the full accumulated transcript (`build_history_messages()`), not just the latest message — this is what lets Claude answer using something Pi said earlier in the same session, and vice versa. The raw executor API (`run_turn()`) has no automatic carry-over between separate executor instances; the caller has to supply the whole conversation itself.
- **Plan mode.** Claude gets a native `permission_mode="plan"` (confirmed to genuinely change behavior — it attempts `ExitPlanMode` / writes a plan doc instead of editing directly). Pi has no native equivalent (confirmed absent from its executor source), so plan mode there is a system-prompt instruction ("describe a plan, don't act") — the same instruction is added for Claude too, for consistent output shape across harnesses. See `omnigent-harness-extensions.md` for the full writeup of both extension points.
- **In-memory sessions.** No persistence; all data lost on restart.
- **Prototype scope.** Validates architectural feasibility for Hypatia; not production-ready.
