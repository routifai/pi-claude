import os
import uuid
from typing import Literal
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
from omnigent import load_agent_def, ClaudeSDKExecutor
from omnigent.inner.pi_executor import PiExecutor
import pathlib

load_dotenv()

# Ensure ANTHROPIC_API_KEY is available for Pi/other harnesses
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if ANTHROPIC_API_KEY:
    os.environ["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load agent definitions from YAML
PROJECT_ROOT = pathlib.Path(__file__).parent.parent
AGENT_CONFIGS = {
    "claude": load_agent_def(PROJECT_ROOT / "agents" / "claude.yaml"),
    "pi": load_agent_def(PROJECT_ROOT / "agents" / "pi.yaml"),
}

# Executor classes for each harness
EXECUTOR_CLASSES = {
    "claude-sdk": ClaudeSDKExecutor,
    "pi": PiExecutor,
}

# Track sessions and executors
sessions_map = {}
# Keyed by (prototype_session_id, harness) - NOT just harness.
# A global per-harness executor would leak one session's conversation
# state into every other session using the same harness.
executors = {}


class MessageInput(BaseModel):
    text: str


class HarnessSwitch(BaseModel):
    harness: Literal["claude", "pi"]


async def get_harness_reply(session_id: str, harness: str, user_text: str) -> str:
    """Query harness using proper executor, scoped to this prototype session."""
    try:
        agent_def = AGENT_CONFIGS.get(harness)
        if not agent_def:
            return f"Error: Unknown harness '{harness}'"

        harness_type = agent_def.executor.harness
        executor_class = EXECUTOR_CLASSES.get(harness_type)
        if not executor_class:
            return f"Error: No executor for harness {harness_type}"

        # Create or reuse executor - one per (session, harness) pair so
        # concurrent prototype sessions never share conversation state.
        executor_key = (session_id, harness)
        if executor_key not in executors:
            executors[executor_key] = executor_class(agent_name=agent_def.name)

        executor = executors[executor_key]

        # Run turn with user message
        messages = [{"role": "user", "content": user_text}]
        response_text = ""

        async for event in executor.run_turn(
            messages=messages,
            tools=[],
            system_prompt=agent_def.prompt or "You are a helpful assistant."
        ):
            if hasattr(event, "text") and event.text:
                response_text += str(event.text)
            elif hasattr(event, "message") and event.message:
                # ExecutorError and similar events carry a human-readable message
                print(f"harness event without text on {harness}: {event}")

        return response_text.strip() or "No response"
    except Exception as e:
        import traceback
        print(f"ERROR querying {harness} for session {session_id}: {e}\n{traceback.format_exc()}")
        return f"Error: {str(e)}"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/sessions")
async def create_session():
    """Create new session (Claude by default)."""
    session_id = str(uuid.uuid4())
    sessions_map[session_id] = {
        "active_harness": "claude",
        "messages": []
    }
    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Get session transcript and harness."""
    if session_id not in sessions_map:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions_map[session_id]
    return {
        "id": session_id,
        "messages": session["messages"],
        "active_harness": session["active_harness"],
    }


@app.post("/api/sessions/{session_id}/message")
async def send_message(session_id: str, input_data: MessageInput):
    """Send message to active harness."""
    if session_id not in sessions_map:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions_map[session_id]
    user_text = input_data.text
    active_harness = session["active_harness"]

    # Add user message
    session["messages"].append({
        "role": "user",
        "text": user_text,
        "harness": None
    })

    # Get response from harness
    reply_text = await get_harness_reply(session_id, active_harness, user_text)

    # Add assistant reply with harness tag
    session["messages"].append({
        "role": "assistant",
        "text": reply_text,
        "harness": active_harness
    })

    return {
        "session_id": session_id,
        "reply": reply_text,
        "harness": active_harness,
    }


@app.post("/api/sessions/{session_id}/switch")
async def switch_harness(session_id: str, switch_data: HarnessSwitch):
    """Switch active harness."""
    if session_id not in sessions_map:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions_map[session_id]
    session["active_harness"] = switch_data.harness

    return {
        "session_id": session_id,
        "active_harness": session["active_harness"],
        "message": f"Switched to {switch_data.harness}",
    }
