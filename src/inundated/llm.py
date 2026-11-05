"""Minimal OpenAI-compatible chat client with JSON output and a sqlite response cache."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time

import httpx

from .config import Backend

_lock = threading.Lock()


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        return json.loads(m.group(0))
    raise LLMError(f"no JSON in response: {text[:200]!r}")


class Client:
    def __init__(self, backend: Backend, con=None):
        self.b = backend
        self.con = con
        self.http = httpx.Client(timeout=backend.timeout)
        self.tokens_in = self.tokens_out = self.calls = self.cache_hits = 0

    def chat_json(self, system: str, user: str, retries: int = 3) -> dict:
        k = hashlib.sha256(f"{self.b.model}\n{system}\n{user}".encode()).hexdigest()
        if self.con is not None:
            with _lock:
                row = self.con.execute("SELECT response FROM llm_cache WHERE k=?", (k,)).fetchone()
            if row:
                self.cache_hits += 1
                return _extract_json(row[0])
        if not self.b.base_url or not self.b.model:
            raise LLMError(f"backend '{self.b.name}' has no base_url/model configured")
        body = {
            "model": self.b.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.b.temperature,
            "max_tokens": self.b.max_tokens,
        }
        body.update(self.b.extra_body)
        if self.b.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", **self.b.extra_headers}
        if self.b.api_key:
            headers["Authorization"] = f"Bearer {self.b.api_key}"
        url = self.b.base_url.rstrip("/") + "/chat/completions"
        err = None
        for attempt in range(retries):
            try:
                r = self.http.post(url, json=body, headers=headers)
                if r.status_code == 400 and self.b.json_mode and "response_format" in body:
                    body.pop("response_format")  # provider doesn't support it; rely on prompt
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt * 2)
                    err = f"HTTP {r.status_code}: {r.text[:200]}"
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                text = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage") or {}
                parsed = _extract_json(text)
                self.calls += 1
                self.tokens_in += usage.get("prompt_tokens", 0)
                self.tokens_out += usage.get("completion_tokens", 0)
                if self.con is not None:
                    with _lock, self.con:
                        self.con.execute(
                            "INSERT OR REPLACE INTO llm_cache(k,backend,model,prompt,response,tokens_in,tokens_out)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (k, self.b.name, self.b.model, user, text,
                             usage.get("prompt_tokens"), usage.get("completion_tokens")))
                return parsed
            except (httpx.HTTPError, json.JSONDecodeError, LLMError, KeyError) as e:
                err = str(e)
                if isinstance(e, LLMError) and str(e).startswith("HTTP 4"):
                    raise
                time.sleep(1 + attempt)
        raise LLMError(err or "unknown error")


class SystemOneClient:
    """Decision-model client (Clef / Jev style): POST {base_url}/systemone with typed questions.

    Non-generative: every option of every question is scored in one pass over `state`, billed on
    input tokens only. Question types: noul (yes/no -> P(true)), choice (pick one of criteria
    keys), score (ordered levels, criteria = list lowest-first).
    """

    def __init__(self, backend: Backend, con=None):
        self.b = backend
        self.con = con
        self.http = httpx.Client(timeout=backend.timeout)
        self.tokens_in = self.tokens_out = self.calls = self.cache_hits = 0
        self.truncated = 0

    def decide(self, state, questions: dict, retries: int = 3) -> dict:
        payload = json.dumps({"q": questions, "s": state}, sort_keys=True, ensure_ascii=False)
        k = hashlib.sha256(f"systemone\n{self.b.model}\n{payload}".encode()).hexdigest()
        if self.con is not None:
            with _lock:
                row = self.con.execute("SELECT response FROM llm_cache WHERE k=?", (k,)).fetchone()
            if row:
                self.cache_hits += 1
                return json.loads(row[0])["answers"]
        if not self.b.base_url or not self.b.model:
            raise LLMError(f"backend '{self.b.name}' has no base_url/model configured")
        headers = {"Content-Type": "application/json", **self.b.extra_headers}
        if self.b.api_key:
            headers["Authorization"] = f"Bearer {self.b.api_key}"
        url = self.b.base_url.rstrip("/") + "/systemone"
        body = {"model": self.b.model, "state": state, "questions": questions}
        err = None
        for attempt in range(retries):
            try:
                r = self.http.post(url, json=body, headers=headers)
                if r.status_code in (429, 500, 502, 503, 504):
                    err = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(2 ** attempt * 2)
                    continue
                if r.status_code == 404:
                    raise LLMError(f"HTTP 404 (decision models are preview-only; is {self.b.model} enabled "
                                   f"for this key? see portal.neuralwatt.com enroll page): {r.text[:200]}")
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                data = r.json()
                usage = data.get("usage") or {}
                self.calls += 1
                self.tokens_in += usage.get("input_tokens", 0)
                self.tokens_out += usage.get("output_tokens", 0)
                self.truncated += bool(data.get("truncated"))
                if self.con is not None:
                    with _lock, self.con:
                        self.con.execute(
                            "INSERT OR REPLACE INTO llm_cache(k,backend,model,prompt,response,tokens_in,tokens_out)"
                            " VALUES (?,?,?,?,?,?,?)",
                            (k, self.b.name, self.b.model, payload, json.dumps(data),
                             usage.get("input_tokens"), usage.get("output_tokens")))
                return data["answers"]
            except (httpx.HTTPError, json.JSONDecodeError, KeyError) as e:
                err = str(e)
                time.sleep(1 + attempt)
        raise LLMError(err or "unknown error")


def make_client(backend: Backend, con=None):
    return SystemOneClient(backend, con) if backend.type == "systemone" else Client(backend, con)
