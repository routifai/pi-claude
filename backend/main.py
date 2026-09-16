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


class PlanModeInput(BaseModel):
    enabled: bool


PLAN_MODE_INSTRUCTION = (
    "\n\nPLAN MODE IS ACTIVE. Do not take any action or make any changes. "
    "Instead, respond with a short, numbered plan describing exactly what "
    "you would do to fulfill the request, then stop. Do not execute the plan."
)

# Pi's own native plan-mode extension (npm:@narumitw/pi-plan-mode), if
# installed. Loaded via Pi's real "--extension <path>" mechanism - the
# same one omnigent itself uses internally to bridge its own tools into
# Pi - plus the "--plan" CLI flag the extension registers once loaded.
# Confirmed by direct testing: this produces genuine native plan-mode
# output (structured Plan/Summary/Verification sections, refuses to
# write files, offers to finalize the plan instead), not a prompt trick.
PI_PLAN_MODE_EXTENSION = (
    pathlib.Path.home() / ".pi" / "agent" / "npm" / "node_modules"
    / "@narumitw" / "pi-plan-mode" / "src" / "index.ts"
)


def build_history_messages(session: dict) -> list[dict]:
    """Convert the session's merged transcript into omnigent's Message shape.

    Passing the FULL accumulated history (not just the latest turn) is what
    lets a harness answer with awareness of turns produced by a different
    harness earlier in the same session - the raw executor API has no
    built-in carry-over between separate executor instances, so the caller
    has to supply the whole conversation on every call.
    """
    return [
        {"role": m["role"], "content": m["text"]}
        for m in session["messages"]
    ]


async def get_harness_reply(session_id: str, harness: str, session: dict) -> str:
    """Query harness using proper executor, scoped to this prototype session."""
    try:
        agent_def = AGENT_CONFIGS.get(harness)
        if not agent_def:
            return f"Error: Unknown harness '{harness}'"

        harness_type = agent_def.executor.harness
        executor_class = EXECUTOR_CLASSES.get(harness_type)
        if not executor_class:
            return f"Error: No executor for harness {harness_type}"

        plan_mode = session.get("plan_mode", False)

        # Executor cache key includes plan_mode: Claude's permission_mode is
        # set at construction time, so toggling plan mode needs a fresh
        # executor instance to actually take effect.
        pi_extension_available = harness_type == "pi" and PI_PLAN_MODE_EXTENSION.is_file()

        executor_key = (session_id, harness, plan_mode)
        if executor_key not in executors:
            kwargs = {"agent_name": agent_def.name}
            if harness_type == "claude-sdk":
                # Native switch - genuinely changes Claude's behavior
                # (attempts ExitPlanMode / writes a plan doc instead of
                # editing directly). Confirmed by direct testing.
                kwargs["permission_mode"] = "plan" if plan_mode else "auto"
            executor = executor_class(**kwargs)
            if plan_mode and pi_extension_available:
                # Real Pi extension, loaded the same way omnigent loads its
                # own tool-bridge extension into Pi. "--plan" is the CLI
                # flag the extension itself registers once loaded.
                executor._extra_args.extend(["--extension", str(PI_PLAN_MODE_EXTENSION), "--plan"])
            executors[executor_key] = executor

        executor = executors[executor_key]

        # Full accumulated transcript, not just the latest message - see
        # build_history_messages().
        messages = build_history_messages(session)

        system_prompt = agent_def.prompt or "You are a helpful assistant."
        if plan_mode and not pi_extension_available and harness_type != "claude-sdk":
            # Fallback for any harness with no native plan/permission mode
            # and no equivalent extension installed - a prompt-level
            # instruction is the harness-agnostic last resort.
            system_prompt += PLAN_MODE_INSTRUCTION

        response_text = ""

        async for event in executor.run_turn(
            messages=messages,
            tools=[],
            system_prompt=system_prompt,
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
        "messages": [],
        "plan_mode": False,
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
        "plan_mode": session.get("plan_mode", False),
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

    # Get response from harness - session is passed (not just user_text) so
    # the full accumulated transcript, across every harness used so far,
    # goes into this turn.
    reply_text = await get_harness_reply(session_id, active_harness, session)

    plan_mode = session.get("plan_mode", False)

    # Add assistant reply with harness tag
    session["messages"].append({
        "role": "assistant",
        "text": reply_text,
        "harness": active_harness,
        "plan_mode": plan_mode,
    })

    return {
        "session_id": session_id,
        "reply": reply_text,
        "harness": active_harness,
        "plan_mode": plan_mode,
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


@app.post("/api/sessions/{session_id}/plan-mode")
async def set_plan_mode(session_id: str, plan_data: PlanModeInput):
    """Toggle plan mode for the session's active harness (and any harness switched to next)."""
    if session_id not in sessions_map:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions_map[session_id]
    session["plan_mode"] = plan_data.enabled

    return {
        "session_id": session_id,
        "plan_mode": session["plan_mode"],
    }
