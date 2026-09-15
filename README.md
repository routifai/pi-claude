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

## Test the Success Criterion

1. Open `http://localhost:5173`.
2. Click "New Session".
3. Type a message → reply appears tagged "claude".
4. Click the "Pi" button to switch harness.
5. Type another message → reply appears tagged "pi", **in the same visible thread**.
6. Reload the page / click "New Session" → both messages still there with correct harness tags.

## Notes

- **Multi-harness architecture validated.** Backend routes each harness to separate executor instances, merges replies into single transcript. Switching works cleanly.
- **Claude SDK only.** Current backend uses `ClaudeSDKExecutor` for both Claude and Pi agents. The `harness: pi` field in Pi agent YAML is parsed but not acted upon — ClaudeSDKExecutor always uses Claude SDK. Real Pi support would require either:
  - PiExecutor (needs `pi --mode rpc` running separately)
  - omnigent HTTP API (bypass Python executors)
- **In-memory sessions.** No persistence; all data lost on restart.
- **Prototype scope.** Validates architectural feasibility for Hypatia; not production-ready.
