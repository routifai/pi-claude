import os
import uuid
from typing import Literal
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import yaml
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

# --- Pi-only plan-mode augmentation -----------------------------------
# Claude needs nothing here: its plan mode is native to claude-sdk
# (ClaudeSDKExecutor's own `permission_mode` parameter, set below).
# Pi has no native equivalent, so its plan mode comes from loading its
# real, installed extension (npm:@narumitw/pi-plan-mode) via Pi's own
# "--extension <path>" CLI mechanism, plus the "--plan" flag that
# extension registers once loaded. Confirmed by direct testing: this
# produces genuine native plan-mode output (structured Plan/Summary/
# Verification, refuses to write files, offers to finalize), not a
# text substitute.
#
# The extension path is sourced from omnigent's own documented config
# location - harness.pi-native.args in .omnigent/config.yaml - not
# hardcoded here. See .omnigent/config.yaml at the project root.
# https://omnigent.ai/docs/build/harnesses/configuration

def load_pi_native_args() -> list[str]:
    """Read harness.pi-native.args from omnigent's own config file.

    Project config (.omnigent/config.yaml) takes precedence over user
    config (~/.omnigent/config.yaml), matching omnigent's own documented
    precedence. Omnigent's *server* reads this same key when it launches
    a native harness session; this backend drives PiExecutor directly
    instead, so reading the file here doesn't get it "for free" from the
    server - it lets the extension's path live in one config file that
    means the same thing whether Pi is launched by this backend or,
    eventually, by the real server.
    """
    for config_path in (
        PROJECT_ROOT / ".omnigent" / "config.yaml",
        pathlib.Path.home() / ".omnigent" / "config.yaml",
    ):
        if not config_path.is_file():
            continue
        try:
            config = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError:
            continue
        args = config.get("harness", {}).get("pi-native", {}).get("args", [])
        if args:
            return args
    return []


PI_NATIVE_ARGS = load_pi_native_args()


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
        pi_augmentation_available = harness_type == "pi" and bool(PI_NATIVE_ARGS)

        # Executor cache key includes plan_mode: Claude's permission_mode is
        # set at construction time, so toggling plan mode needs a fresh
        # executor instance to actually take effect.
        executor_key = (session_id, harness, plan_mode)
        if executor_key not in executors:
            kwargs = {"agent_name": agent_def.name}
            if harness_type == "claude-sdk":
                # Native switch - genuinely changes Claude's behavior
                # (attempts ExitPlanMode / writes a plan doc instead of
                # editing directly). Confirmed by direct testing.
                kwargs["permission_mode"] = "plan" if plan_mode else "auto"
            executor = executor_class(**kwargs)
            if plan_mode and pi_augmentation_available:
                # Pi-only: append harness.pi-native.args from
                # .omnigent/config.yaml (--extension <path> --plan).
                executor._extra_args.extend(PI_NATIVE_ARGS)
            executors[executor_key] = executor

        executor = executors[executor_key]

        # Full accumulated transcript, not just the latest message - see
        # build_history_messages().
        messages = build_history_messages(session)

        system_prompt = agent_def.prompt or "You are a helpful assistant."
        if plan_mode and not pi_augmentation_available and harness_type != "claude-sdk":
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
