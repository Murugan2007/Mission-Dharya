"""
FlowSync — LLM Tool Orchestration
==================================
Defines the tool schemas and provides both a real LLM
integration (Gemini API) and a mock fallback for demos
where an API key isn't available.

The key principle: the LLM is a reasoning and explanation
layer, NOT a decision-maker. It calls deterministic tools
(like what_if) to get hard numbers, then explains those
numbers to the operator in plain English.

This is what the problem statement means by "an LLM acting
as a reasoning and tool-orchestration layer, not just a
chatbot."
"""

import os
import json
import logging

logger = logging.getLogger("flowsync.llm")

# ─── Tool Schemas ────────────────────────────────────────────
# Compatible with Gemini function calling format.

TOOL_SCHEMAS = [
    {
        "name": "get_junction_state",
        "description": "Get the current state of a specific junction, including its signal phase, queue lengths, sensor health, and operating mode.",
        "parameters": {
            "type": "object",
            "properties": {
                "junction_id": {
                    "type": "string",
                    "enum": ["J1", "J2", "J3"],
                    "description": "Which junction to query"
                }
            },
            "required": ["junction_id"]
        }
    },
    {
        "name": "get_queues",
        "description": "Get the current queue lengths at all junctions.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "get_ev_status",
        "description": "Get the emergency vehicle's current position, speed, route, and ETA. Restricted to operators.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "what_if",
        "description": "Run a deterministic what-if analysis. Returns computed impact numbers for a proposed action at a junction. Use this before recommending preemption or phase changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "junction_id": {
                    "type": "string",
                    "enum": ["J1", "J2", "J3"],
                    "description": "Junction to analyze"
                },
                "action": {
                    "type": "string",
                    "enum": ["preempt", "extend_green"],
                    "description": "What action to simulate"
                }
            },
            "required": ["junction_id", "action"]
        }
    },
    {
        "name": "propose_phase_change",
        "description": "Propose a signal phase change at a junction. The MCU will validate against safety rules before applying. Restricted to operators.",
        "parameters": {
            "type": "object",
            "properties": {
                "junction_id": {
                    "type": "string",
                    "enum": ["J1", "J2", "J3"]
                },
                "phase": {
                    "type": "string",
                    "enum": ["EW_GREEN", "NS_GREEN"],
                    "description": "Desired phase"
                },
                "duration_ms": {
                    "type": "integer",
                    "description": "Green duration in milliseconds (10000-60000)"
                }
            },
            "required": ["junction_id", "phase", "duration_ms"]
        }
    },
    {
        "name": "report_incident",
        "description": "Report a traffic incident at a junction. Restricted to operators.",
        "parameters": {
            "type": "object",
            "properties": {
                "junction_id": {
                    "type": "string",
                    "enum": ["J1", "J2", "J3"]
                },
                "description": {
                    "type": "string",
                    "description": "What happened — e.g., 'stalled vehicle blocking east approach'"
                }
            },
            "required": ["junction_id", "description"]
        }
    },
    {
        "name": "suggest_alt_route",
        "description": "Get an alternative route suggestion when a junction is blocked.",
        "parameters": {
            "type": "object",
            "properties": {
                "blocked_junction": {
                    "type": "string",
                    "enum": ["J1", "J2", "J3"]
                }
            },
            "required": ["blocked_junction"]
        }
    },
]


# ─── Tool Executor ───────────────────────────────────────────
# Maps tool names to core methods. The LLM says "call X",
# we actually call X on the FlowSyncCore, and return the result.

class ToolExecutor:
    def __init__(self, core, role):
        self.core = core
        self.role = role

    def execute(self, tool_name: str, args: dict) -> dict:
        handlers = {
            "get_junction_state": lambda: self.core.tool_get_junction_state(
                args.get("junction_id", "J1"), self.role),
            "get_queues": lambda: self.core.tool_get_queues(self.role),
            "get_ev_status": lambda: self.core.tool_get_ev_status(self.role),
            "what_if": lambda: self.core.tool_what_if(
                args.get("junction_id", "J1"),
                args.get("action", "preempt"), self.role),
            "propose_phase_change": lambda: self.core.tool_propose_phase_change(
                args.get("junction_id", "J1"),
                args.get("phase", "EW_GREEN"),
                args.get("duration_ms", 25000), self.role),
            "report_incident": lambda: self.core.tool_report_incident(
                args.get("junction_id", "J1"),
                args.get("description", ""), self.role),
            "suggest_alt_route": lambda: self.core.tool_suggest_alt_route(
                args.get("blocked_junction", "J1"), self.role),
        }

        handler = handlers.get(tool_name)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            return handler()
        except Exception as e:
            logger.error(f"Tool execution error: {tool_name}: {e}")
            return {"error": str(e)}


# ─── Mock LLM (for demos without an API key) ────────────────
# Pattern-matches on keywords and calls the right tools.
# It's not smart, but it demonstrates the tool-calling flow.

class MockLLM:
    """
    A keyword-based mock that demonstrates the LLM → tool → response
    flow without requiring an API key. Good enough for a demo.
    """

    def __init__(self, core, role):
        self.executor = ToolExecutor(core, role)
        self.core = core

    def ask(self, question: str) -> dict:
        q = question.lower().strip()
        tool_calls = []
        response = ""

        # ── "Should I preempt?" questions ──
        if any(kw in q for kw in ["preempt", "should i", "what if", "impact"]):
            # Figure out which junction they're asking about
            jid = "J2"  # default
            for j in ["J1", "J2", "J3"]:
                if j.lower() in q:
                    jid = j
                    break

            result = self.executor.execute("what_if", {
                "junction_id": jid, "action": "preempt"})
            tool_calls.append({"tool": "what_if", "args": {"junction_id": jid, "action": "preempt"}, "result": result})

            if "error" in result:
                response = f"I ran into an issue: {result['error']}"
            else:
                rec = result.get("recommendation", "unknown")
                cars = result.get("cars_affected", 0)
                delay = result.get("est_delay_per_car_s", 0)
                reasoning = result.get("reasoning", "")

                if rec == "preempt":
                    response = (f"**Recommendation: Go ahead with preemption at {jid}.**\n\n"
                                f"{reasoning}\n\n"
                                f"Impact: {cars} vehicles would be delayed by ~{delay}s each. "
                                f"That's manageable given the emergency.")
                else:
                    response = (f"**Recommendation: Hold off on preempting {jid} for now.**\n\n"
                                f"{reasoning}\n\n"
                                f"Consider waiting for the current phase to complete, "
                                f"or use the alternate route suggestion.")

        # ── Status / what's happening questions ──
        elif any(kw in q for kw in ["status", "what's happening", "overview", "how are", "situation"]):
            results = {}
            for jid in ["J1", "J2", "J3"]:
                r = self.executor.execute("get_junction_state", {"junction_id": jid})
                results[jid] = r
                tool_calls.append({"tool": "get_junction_state", "args": {"junction_id": jid}, "result": r})

            lines = ["**Current system status:**\n"]
            for jid, r in results.items():
                phase = r.get("phase", "unknown")
                mode = r.get("mode", "unknown")
                sensor = "✓ OK" if r.get("sensor_ok", True) else "⚠ FAULT"
                queues = r.get("queues", {})
                q_str = ", ".join(f"{k}: {v}" for k, v in queues.items()) if queues else "no data"
                lines.append(f"- **{jid}**: {phase} ({mode}) | Sensors: {sensor} | Queues: {q_str}")

            ev = self.executor.execute("get_ev_status", {})
            tool_calls.append({"tool": "get_ev_status", "args": {}, "result": ev})
            if "error" not in ev:
                if ev.get("active"):
                    lines.append(f"\n🚑 **Emergency vehicle active** near {ev.get('nearest')} "
                                 f"at {ev.get('speed_kmh', 0)} km/h")
                else:
                    lines.append("\n🚑 No active emergency vehicle.")

            response = "\n".join(lines)

        # ── Incident questions ──
        elif any(kw in q for kw in ["incident", "accident", "blocked", "stalled"]):
            # Try to extract junction
            jid = "J1"
            for j in ["J1", "J2", "J3"]:
                if j.lower() in q:
                    jid = j
                    break

            alt = self.executor.execute("suggest_alt_route", {"blocked_junction": jid})
            tool_calls.append({"tool": "suggest_alt_route", "args": {"blocked_junction": jid}, "result": alt})

            state = self.executor.execute("get_junction_state", {"junction_id": jid})
            tool_calls.append({"tool": "get_junction_state", "args": {"junction_id": jid}, "result": state})

            suggestion = alt.get("suggestion", "No suggestion available.")
            extra = alt.get("estimated_extra_time_s", 0)

            response = (f"**Incident analysis for {jid}:**\n\n"
                        f"Current phase: {state.get('phase', 'unknown')}, "
                        f"mode: {state.get('mode', 'unknown')}\n\n"
                        f"**Alternate route:** {suggestion}\n"
                        f"Extra travel time: ~{extra}s\n\n"
                        f"I'd recommend reporting this formally so it appears "
                        f"on the public dashboard. Want me to do that?")

        # ── EV tracking questions ──
        elif any(kw in q for kw in ["ambulance", "emergency", "ev ", "vehicle"]):
            ev = self.executor.execute("get_ev_status", {})
            tool_calls.append({"tool": "get_ev_status", "args": {}, "result": ev})

            if "error" in ev:
                response = f"Access issue: {ev['error']}"
            elif ev.get("active"):
                etas = ev.get("etas", {})
                eta_str = ", ".join(f"{k}: {v}s" for k, v in etas.items()) if etas else "calculating..."
                response = (f"🚑 **Emergency vehicle is active.**\n\n"
                            f"- Position: near {ev.get('nearest', '?')}\n"
                            f"- Speed: {ev.get('speed_kmh', 0)} km/h\n"
                            f"- Route: {' → '.join(ev.get('route', []))}\n"
                            f"- ETAs: {eta_str}\n\n"
                            f"Pre-clearing is being handled automatically.")
            else:
                response = "🚑 No emergency vehicle is currently active in the system."

        # ── Default help ──
        else:
            response = ("I'm the FlowSync traffic intelligence assistant. "
                        "I can help you with:\n\n"
                        "- **\"What's the current status?\"** — overview of all junctions\n"
                        "- **\"Should I preempt J2?\"** — what-if analysis with impact numbers\n"
                        "- **\"There's an incident at J1\"** — incident analysis + alt routes\n"
                        "- **\"Where's the ambulance?\"** — EV tracking (operator only)\n\n"
                        "I use real data from the junction sensors and run deterministic "
                        "simulations — I don't guess or make up numbers.")

        return {
            "response": response,
            "tool_calls": tool_calls,
        }


# ─── Real LLM (Gemini API) ──────────────────────────────────

class GeminiLLM:
    """
    Uses Google's Gemini API with function calling.
    Falls back to MockLLM if the API key isn't set or the call fails.
    """

    def __init__(self, core, role):
        self.core = core
        self.role = role
        self.executor = ToolExecutor(core, role)
        self.mock = MockLLM(core, role)
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client = None

        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
                logger.info("Gemini API client initialized")
            except Exception as e:
                logger.warning(f"Couldn't init Gemini client: {e}. Using mock.")
                self.client = None

    def ask(self, question: str) -> dict:
        if not self.client:
            return self.mock.ask(question)

        try:
            return self._ask_gemini(question)
        except Exception as e:
            logger.error(f"Gemini API error: {e}. Falling back to mock.")
            result = self.mock.ask(question)
            result["note"] = f"(Used mock — Gemini error: {str(e)[:100]})"
            return result

    def _ask_gemini(self, question: str):
        from google import genai
        from google.genai import types

        # Build tool declarations for Gemini
        tool_declarations = []
        for schema in TOOL_SCHEMAS:
            props = {}
            required = []
            params = schema.get("parameters", {})
            for pname, pdef in params.get("properties", {}).items():
                prop_schema = {"type": pdef.get("type", "string").upper()}
                if "description" in pdef:
                    prop_schema["description"] = pdef["description"]
                if "enum" in pdef:
                    prop_schema["enum"] = pdef["enum"]
                props[pname] = types.Schema(**prop_schema)
            required = params.get("required", [])

            func_decl = types.FunctionDeclaration(
                name=schema["name"],
                description=schema["description"],
                parameters=types.Schema(
                    type="OBJECT",
                    properties=props,
                    required=required,
                ) if props else None,
            )
            tool_declarations.append(func_decl)

        tools = [types.Tool(function_declarations=tool_declarations)]

        system_prompt = (
            "You are FlowSync, an AI traffic intelligence assistant for a city "
            "traffic control center. You help operators understand the current "
            "traffic situation and make decisions about signal timing, emergency "
            "vehicle preemption, and incident response.\n\n"
            "RULES:\n"
            "1. Always call the what_if tool before recommending preemption. "
            "Never make up impact numbers.\n"
            "2. Never tell drivers to run red lights or make unsafe manoeuvres.\n"
            "3. When reporting numbers, always say they came from the tool.\n"
            "4. Be concise and direct — operators are busy.\n"
            "5. Use markdown formatting for clarity."
        )

        all_tool_calls = []

        # Initial request
        response = self.client.models.generate_content(
            model="gemini-2.0-flash",
            contents=question,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=tools,
                temperature=0.3,
            ),
        )

        # Handle function calls (up to 3 rounds)
        for _ in range(3):
            if not response.candidates or not response.candidates[0].content.parts:
                break

            func_calls = [p for p in response.candidates[0].content.parts
                          if p.function_call]
            if not func_calls:
                break

            # Execute each function call
            func_responses = []
            for fc in func_calls:
                args = dict(fc.function_call.args) if fc.function_call.args else {}
                result = self.executor.execute(fc.function_call.name, args)
                all_tool_calls.append({
                    "tool": fc.function_call.name,
                    "args": args,
                    "result": result,
                })
                func_responses.append(types.Part.from_function_response(
                    name=fc.function_call.name,
                    response=result,
                ))

            # Send results back for next round
            response = self.client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[
                    types.Content(role="user", parts=[types.Part.from_text(text=question)]),
                    response.candidates[0].content,
                    types.Content(role="tool", parts=func_responses),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=tools,
                    temperature=0.3,
                ),
            )

        # Extract text response
        text = ""
        if response.candidates and response.candidates[0].content.parts:
            text_parts = [p.text for p in response.candidates[0].content.parts if p.text]
            text = "\n".join(text_parts)

        return {
            "response": text or "I processed your request but couldn't generate a response.",
            "tool_calls": all_tool_calls,
        }


def create_llm(core, role):
    """Factory: returns GeminiLLM if API key is set, otherwise MockLLM."""
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiLLM(core, role)
    return MockLLM(core, role)
