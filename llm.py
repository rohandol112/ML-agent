import json
import os

from groq import BadRequestError

from schema import Decision
from tools import TOOLS

MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
TOKEN_LIMIT = int(os.getenv("GROQ_TOKEN_LIMIT", "8000"))


class GroqChat:
    def __init__(self, client, system, prompt):
        self.client = client
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def _fit(self, extra_tokens):
        budget = (TOKEN_LIMIT - extra_tokens) * 3
        while len(json.dumps(self.messages)) > budget:
            longest = max((m for m in self.messages if m["role"] == "tool"), key=lambda m: len(m["content"]), default=None)
            if not longest or len(longest["content"]) < 400:
                break
            longest["content"] = longest["content"][: len(longest["content"]) // 2] + " ...[cut to fit the token limit]"

    def _call(self, max_tokens, **kwargs):
        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=MODEL, messages=self.messages, max_completion_tokens=max_tokens, **kwargs)
                break
            except BadRequestError as exc:
                if "tool_use_failed" not in str(exc) or attempt == 2:
                    raise
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise RuntimeError("Groq stopped early: max tokens")
        message = choice.message
        self.messages.append({"role": "assistant", "content": message.content or "", **({"tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in message.tool_calls]} if message.tool_calls else {})})
        return message

    def turn(self):
        self._fit(extra_tokens=2000 + len(json.dumps(TOOLS)) // 3)
        calls = []
        for c in self._call(2000, tools=TOOLS).tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append((c.id, c.function.name, args))
        return calls

    def send_results(self, results):
        self.messages += [{"role": "tool", "tool_call_id": call_id, "content": json.dumps(output)}
                          for call_id, _, output in results]

    def final(self, instruction):
        self.messages.append({"role": "user", "content": f"{instruction}\nDo not call any tools now; reply only with the JSON."})
        schema = Decision.model_json_schema()
        self._fit(extra_tokens=3000 + len(json.dumps(schema)) // 3)
        return self._call(3000, response_format={"type": "json_schema", "json_schema": {
            "name": "Decision", "schema": schema, "strict": True}}).content
