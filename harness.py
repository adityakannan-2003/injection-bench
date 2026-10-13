"""Run one episode: a model works a single task in a world, with one attack planted and one defense
applied, and we record what it did.

Two outcomes are the point:
- obeyed: the model emitted the forbidden call the attack asked for (it was fooled).
- harm:   that call actually executed (the rulebook, if on, didn't stop it).

With no rulebook, obeyed and harm are the same. With the rulebook, harm can be lower: the model is
fooled but the call is blocked or held for a person. The task outcome (done / held / failed) says
whether the real job still got done, so we can see a defense that works by breaking the agent.

Backends share one message format so the same loop drives a Claude model over the API and a local
model over Ollama. A third, scripted backend lives in the tests, so the loop runs with no API, no
network and no cost.
"""

import json
import re
import time
from dataclasses import dataclass, field

from attacks import Attack
from defenses import Defense, wrap_untrusted
from worlds import Call, World

MAX_ROUNDS = 8


@dataclass
class Episode:
    world: str
    scenario: str
    goal: str
    attack: str
    defense: str
    model: str
    obeyed: bool = False          # the model emitted the attacker's forbidden call
    harm: bool = False            # that call executed (was not blocked or held)
    exposed: bool = False         # a tool result actually showed the model the injected text
    task: str = "failed"          # done / held / failed: did the real job still get done
    rounds: int = 0
    emitted: list = field(default_factory=list)   # [{name, args}] every call the model made
    final_text: str = ""
    error: str = ""
    stops: list = field(default_factory=list)     # why each request ended: tool_use, end_turn, refusal...
    served: list = field(default_factory=list)    # which model actually answered each request
    usage: dict = field(default_factory=dict)
    latency_s: float = 0.0

    def to_row(self) -> dict:
        return {**self.__dict__}


def build_system(world: World, defense: Defense) -> str:
    return world.system + defense.system_suffix


def run_episode(world: World, attack: Attack | None, goal: str | None, defense: Defense,
                backend, max_rounds: int = MAX_ROUNDS) -> Episode:
    """attack/goal are None for a clean run (no injection), used to measure the defense's cost on
    the real task. Otherwise the goal's ask is built into the world's untrusted text by the attack."""
    if attack is not None and goal is not None:
        ask = world.goals[goal]["ask"]
        injection, hide = attack.build(ask)
        world.injection, world.hide = injection, hide
    ep = Episode(world.name, world.scenario, goal or "", attack.name if attack else "none",
                 defense.name, backend.model)
    system = build_system(world, defense)
    messages = [{"role": "user", "content": world.task}]
    executed, held = [], []
    t0 = time.monotonic()
    try:
        for _ in range(max_rounds):
            ep.rounds += 1
            reply = backend.chat(system, messages, world.tools)
            text, calls, usage = reply["text"], reply["calls"], reply["usage"]
            add_usage(ep.usage, usage)
            ep.stops.append(reply.get("stop", ""))
            ep.served.append(reply.get("model", backend.model))
            # raw keeps the provider's own blocks (thinking included), which must go back unchanged.
            assistant = {"role": "assistant", "content": text or "", "tool_calls": calls, "raw": reply.get("raw")}
            messages.append(assistant)
            if not calls:
                ep.final_text = text or ""
                break
            results = []
            for call in calls:
                c = Call(call["name"], call.get("args") or {}, call.get("id", ""))
                ep.emitted.append({"name": c.name, "args": c.args})
                is_attack_call = goal is not None and world.attack_match(goal, c)
                if is_attack_call:
                    ep.obeyed = True
                output = run_call(world, c, defense, executed, held, flag_harm=is_attack_call, ep=ep)
                results.append({"role": "tool", "id": c.id, "name": c.name, "content": output})
            messages.append({"role": "tool_results", "results": results})
        ep.exposed = world.exposed
        ep.task = world.task_result(executed, held)
    except BackendError as exc:
        ep.error = f"{type(exc).__name__}: {exc}"
    ep.latency_s = round(time.monotonic() - t0, 1)
    return ep


def run_call(world: World, c: Call, defense: Defense, executed: list, held: list, flag_harm: bool, ep: Episode) -> str:
    if defense.apply_rulebook:
        decision = world.rulebook(c)
        if decision.verdict == "deny":
            return f"Blocked by policy: {decision.reason}."
        if decision.verdict == "ask":
            held.append(c)
            return f"Held for a person to approve: {decision.reason}. It has not happened yet."
    executed.append(c)
    if flag_harm:
        ep.harm = True
    output = world.call(c)
    if defense.delimit and c.name in world.untrusted:
        return wrap_untrusted(f"tool:{c.name}", output)
    return output


def add_usage(total: dict, usage: dict) -> None:
    for key, value in usage.items():
        total[key] = total.get(key, 0) + value


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class BackendError(Exception):
    pass


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant" and m.get("raw") is not None:
            out.append({"role": "assistant", "content": m["raw"]})
        elif m["role"] == "assistant":
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for call in m.get("tool_calls") or []:
                content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
            out.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no text)"}]})
        elif m["role"] == "tool_results":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in m["results"]]})
    return out


class AnthropicBackend:
    def __init__(self, model: str, client=None, max_tokens: int = 16000):
        import anthropic

        self.model = model
        self.client = client or anthropic.Anthropic(max_retries=6)
        self.max_tokens = max_tokens
        self._errors = anthropic.APIError

    def chat(self, system: str, messages: list[dict], tools: list[dict]):
        import anthropic

        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=system,
                tools=to_anthropic_tools(tools), messages=to_anthropic_messages(messages),
            )
        except anthropic.APIError as exc:
            raise BackendError(str(exc)) from exc
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [{"id": b.id, "name": b.name, "args": b.input} for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason == "max_tokens":
            calls = []  # a tool call cut off mid-stream can parse as valid-looking input; don't run it
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "requests": 1}
        return {"text": text, "calls": calls, "usage": usage, "stop": resp.stop_reason,
                "model": resp.model, "raw": resp.content}


def to_ollama_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": t} for t in tools]


def to_ollama_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            turn = {"role": "assistant", "content": m.get("content") or ""}
            calls = m.get("tool_calls") or []
            if calls:
                turn["tool_calls"] = [{"function": {"name": c["name"], "arguments": c["args"]}} for c in calls]
            out.append(turn)
        elif m["role"] == "tool_results":
            for r in m["results"]:
                out.append({"role": "tool", "content": r["content"], "tool_name": r["name"]})
    return out


class OllamaBackend:
    def __init__(self, model: str, host: str = "http://localhost:11434", timeout: int = 900):
        self.model = model
        self.url = host.rstrip("/") + "/api/chat"
        self.timeout = timeout
        self._think = True

    def chat(self, system: str, messages: list[dict], tools: list[dict]):
        import urllib.error
        import urllib.request

        body = {"model": self.model, "messages": to_ollama_messages(system, messages),
                "tools": to_ollama_tools(tools), "stream": False}
        if self._think:
            body["think"] = False
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if self._think and "think" in detail.lower():
                self._think = False
                return self.chat(system, messages, tools)
            raise BackendError(detail or f"HTTP {exc.code}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise BackendError(f"can't reach Ollama at {self.url}: {getattr(exc, 'reason', exc)}") from None
        message = data.get("message", {})
        text = _strip_think(message.get("content", ""))
        calls = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": f"ollama-{len(calls)}", "name": fn.get("name", ""),
                          "args": args if isinstance(args, dict) else {}})
        usage = {"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0),
                 "requests": 1}
        return {"text": text, "calls": calls, "usage": usage, "stop": data.get("done_reason", ""),
                "model": self.model, "raw": None}


def _read_error(exc) -> str:
    try:
        return json.loads(exc.read()).get("error", "")
    except Exception:
        return ""


def _strip_think(text: str) -> str:
    text = text or ""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return re.sub(r"<think>.*", "", text, flags=re.S).strip()


def make_backend(model: str, local: bool = False):
    return OllamaBackend(model) if local else AnthropicBackend(model)
