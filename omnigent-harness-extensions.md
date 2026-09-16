# Registering Tools and Overriding Behavior Per Harness in Omnigent

Reference for two extension points: giving a specific harness custom tools, and giving a specific harness behavior it doesn't have natively (using Pi's missing "plan mode" as the worked example). Covers the documented path, where it stops, and what has to be wired manually when driving executors directly instead of through Omnigent's full server/session runtime.

## Contents

1. [Background: Omnigent's real extension points](#background)
2. [Registering custom tools per harness](#tools)
3. [Registering skills per harness](#skills)
4. [Overriding behavior per harness (the plan-mode case)](#behavior)
5. [Summary: native vs. manually wired, per harness](#summary)
6. [Recommended pattern going forward](#recommendation)
7. [References](#references)

---

## Background: Omnigent's real extension points {#background}

Omnigent documents two separate extension points relevant here:

- **Custom Agents** — attach Python-function-backed tools to an agent via YAML ([docs](https://omnigent.ai/docs/use/custom-agents)).
- **Contextual Policies** — Python functions that intercept every tool call, LLM request, or file operation and decide `ALLOW` / `ASK` / `DENY`, in real time, across any harness ([overview](https://omnigent.ai/docs/policies/overview), [custom policies](https://omnigent.ai/docs/policies/custom)).

Both are documented as part of Omnigent's full server/session runtime. When driving a harness executor directly (`ClaudeSDKExecutor`, `PiExecutor`, etc.) rather than through that runtime, the YAML resolves correctly, but the automatic wiring these features rely on has to be reproduced by hand. What follows documents exactly where that gap is and how to close it.

---

## Registering custom tools per harness {#tools}

### The documented pattern

```yaml
name: my_agent
executor:
  harness: pi          # or claude-sdk, codex, etc.
tools:
  get_secret_code:
    type: function
    callable: my_package.tools.get_secret_code
```

`load_agent_def()` resolves the `callable` string into a real, live Python function reference automatically:

```python
from omnigent import load_agent_def

agent_def = load_agent_def("agent.yaml")
agent_def.tools
# {'get_secret_code': FunctionTool(name='get_secret_code', ..., callable=<function get_secret_code at 0x...>)}
```

### The gap at the executor level

`run_turn()` does not accept `FunctionTool` objects directly — passing them raises `'FunctionTool' object has no attribute 'get'`. It expects a plain dict schema:

```python
tool_dicts = [{
    "name": name,
    "description": ft.description or f"Call {name}",
    "input_schema": ft.input_schema or {"type": "object", "properties": {}},
} for name, ft in agent_def.tools.items()]
```

Even with the correct schema shape, calling the tool still fails with `"No tool executor for '<name>'"`. The schema reaches the harness (it correctly requests the tool), but nothing routes the call back to the Python function. Every executor (`pi_executor.py`, `claude_sdk_executor.py`, `codex_executor.py`, `antigravity_executor.py`, `openai_agents_sdk_executor.py`) resolves calls through a private instance attribute with no public setter:

```python
executor = PiExecutor(agent_name=agent_def.name)
executor._tool_executor = dispatcher   # private attribute, no constructor parameter or public setter exists
```

where `dispatcher(name, args)` looks up and invokes the matching callable and returns the result in the shape the executor expects.

### A working dispatcher

```python
callables_by_name = {name: ft.callable for name, ft in agent_def.tools.items()}

def dispatcher(name, args):
    fn = callables_by_name[name]
    return {"content": [{"type": "text", "text": str(fn(**args))}]}

executor._tool_executor = dispatcher
```

Confirmed working end-to-end for Pi: a real `ToolCallRequest` → `ToolCallComplete(status=SUCCESS)` → the harness's final answer containing the exact value the Python function returned.

### One contract difference between harnesses

Pi's tool-executor callback accepts either a synchronous or an `async` function. Claude's requires `async` — a synchronous dispatcher fails with `"object dict can't be used in 'await' expression"`. Use:

```python
async def dispatcher(name, args):
    fn = callables_by_name[name]
    return {"content": [{"type": "text", "text": str(fn(**args))}]}
```

for anything that needs to work across both.

### One delivery difference between harnesses

Confirmed working end-to-end for Claude as well, but through a different mechanism: Claude surfaces custom tools as MCP-style tools, discovered dynamically via its own tool-search call (`mcp__omnigent__<tool_name>`), rather than a direct tool list the way Pi receives them. Same YAML input, same dispatcher pattern, different internal transport — no change required on the calling side.

---

## Registering skills per harness {#skills}

Skills (self-contained directories with a `SKILL.md` file) load through a separate, undocumented-at-the-executor-level path: the `bundle_dir` constructor parameter.

```python
executor = PiExecutor(
    agent_name="my_agent",
    bundle_dir=pathlib.Path("/path/to/bundle"),   # expects <bundle_dir>/skills/<name>/SKILL.md
    skills_filter="all",                           # default; also accepts "none" or a list of names
)
```

Internally, `_resolve_pi_skill_args()` walks `<bundle_dir>/skills/`, and for every subdirectory containing a `SKILL.md`, appends a `--skill <path>` flag to the CLI invocation. `skills_filter="all"` (the default) adds every bundled skill as an explicit flag *and* leaves Pi's own auto-discovery running, so host-installed skills can surface too.

**Important:** asking the harness to introspect and list its own loaded skills is unreliable — a direct "what skills do you have" question can return "none" even when skills are correctly loaded and actively influencing behavior. Confirm skill loading by inspecting the executor's actually-resolved CLI arguments (e.g. `executor._extra_args`) directly, or by giving it a task that should invoke the skill's real content and checking whether the response reflects that content — not by asking the model to self-report.

---

## Overriding behavior per harness (the plan-mode case) {#behavior}

### Claude has a native switch

```python
executor = ClaudeSDKExecutor(agent_name="my_agent", permission_mode="plan")
```

Confirmed to genuinely change behavior: under `permission_mode="plan"`, the executor attempts an `ExitPlanMode` tool call and writes a plan document instead of performing the requested edit directly. Under `permission_mode="auto"` (default), it does neither.

### Pi has no equivalent — confirmed by source inspection, not absence of documentation

There is no `permission_mode` parameter, and no reference to plan/permission modes anywhere in Pi's executor source. Asking the harness directly ("what modes do you have?") reports zero, consistent with the source. This is a genuinely missing capability, not an undiscovered wiring path — unlike the tools case above.

### The real extension point: Contextual Policies

Omnigent's documented Policy system is exactly the right shape for this problem, and it is harness-agnostic by design. A policy function receives an event and returns a decision:

```python
from omnigent.policies.schema import PolicyEvent, PolicyResponse

def my_policy(event: PolicyEvent) -> PolicyResponse | None:
    if event["type"] != "tool_call":
        return None
    if event["data"]["name"] in {"write_file", "edit_file"}:
        return {"result": "DENY", "reason": "writes blocked"}
    return {"result": "ALLOW"}
```

The `event["context"]` field includes `harness` — meaning a single policy function can branch on which harness is running and apply different rules to each, from one place. This is the documented per-harness override mechanism (see [Contextual Policies](https://omnigent.ai/docs/policies/overview), [Custom Policies](https://omnigent.ai/docs/policies/custom)).

Registered via YAML:

```yaml
policy_modules:
  - my_package.policies

policies:
  confine_writes:
    type: function
    handler: my_package.policies.confine_writes
```

### Reproducing it at the executor level

The Policy engine normally intercepts events through Omnigent's full Session/server runtime. Driving `PiExecutor` directly means that layer isn't present, so the same interception has to happen inside the tool dispatcher built for the [tools section](#tools) above — the mechanism is identical, just applied one level down:

```python
def make_policy_dispatcher(harness: str, mode: str):
    def dispatcher(name, args):
        if harness == "pi" and mode == "plan" and name in WRITE_TOOLS:
            return {"content": [{"type": "text", "text": '{"error":"DENIED by plan-mode policy"}'}], "isError": True}
        return execute(name, args)
    return dispatcher
```

Confirmed working: under `mode="plan"`, a write call was denied and the harness's own final response correctly explained the write was blocked and proposed an alternative — the same shape of behavior Claude's native plan mode produces, built from nothing for a harness that has no native concept of it.

### The general template

```python
def dispatcher(name, args):
    if harness == "pi" and mode == "plan" and name in WRITE_TOOLS:
        return deny(...)
    if harness == "claude":
        pass  # native permission_mode already covers this harness
    return execute(name, args)
```

One function, one place, harness-specific rules — a harness with no native mode gets an equivalent built for it; a harness with a native mode keeps using it.

---

## Summary: native vs. manually wired, per harness {#summary}

| Capability | Claude | Pi |
|---|---|---|
| Custom tools | Works — requires dict conversion + `_tool_executor` wiring (private attribute) | Works — same wiring, dispatcher must be `async` |
| Skills | Not tested | Works — `bundle_dir` → auto-resolved `--skill` flags |
| Plan/permission mode | Native — `permission_mode` constructor parameter | Not native — no such parameter or reference in source |
| Plan-mode-equivalent behavior | N/A, already native | Achievable — via a policy-style check inside the tool dispatcher |

Across every capability tested, the pattern repeats: the underlying capability generally exists, but driving an executor directly instead of through Omnigent's full session runtime means whatever that runtime normally wires up automatically — tool execution routing, skill loading confirmation, policy interception — has to be built by hand at the same interception points the runtime itself would use.

---

## Recommended pattern going forward {#recommendation}

Rather than wiring `_tool_executor` ad hoc per agent, a small shared helper is worth building once:

```python
def build_dispatcher(agent_def, harness: str, policy_fn=None):
    """Combines YAML-registered tools with an optional harness-aware policy check."""
    callables_by_name = {name: ft.callable for name, ft in agent_def.tools.items()}

    async def dispatcher(name, args):
        if policy_fn:
            decision = policy_fn(harness=harness, name=name, args=args)
            if decision and decision["result"] == "DENY":
                return {"content": [{"type": "text", "text": decision["reason"]}], "isError": True}
        fn = callables_by_name[name]
        result = fn(**args)
        return {"content": [{"type": "text", "text": str(result)}]}

    return dispatcher
```

This keeps tool registration and behavior overrides in one reusable place, keyed by harness, matching the shape of Omnigent's own documented Policy system closely enough that migrating to the full server runtime later — if that becomes the right call — is a drop-in change rather than a rewrite.

---

## References {#references}

- [Custom Agents · Omnigent docs](https://omnigent.ai/docs/use/custom-agents)
- [Contextual Policies · Omnigent docs](https://omnigent.ai/docs/policies/overview)
- [Custom Policies · Omnigent docs](https://omnigent.ai/docs/policies/custom)
- [AGENT_YAML_SPEC · omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md)
- [Contextual Policies in Omnigent — Databricks Blog](https://www.databricks.com/blog/contextual-policies-omnigent-using-session-state-better-govern-ai-agents)
- [omnigent-ai/omnigent — GitHub](https://github.com/omnigent-ai/omnigent)
