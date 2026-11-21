"""
An LLM gateway built with LiteLLM: one middle layer between ALL your apps and ALL your models.

    your app (RAG, agent, chatbot)
          |
          v
    +-------------------- Gateway --------------------+
    |  1. guardrails     same input/output checks for  |
    |                    every app (security.py)       |
    |  2. cache          same question -> no LLM call  |
    |  3. router         timeout, retry, fallback:     |
    |                    qwen2.5:7b -> gemma2:2b ->    |
    |                    llama3.2:1b                   |
    |  4. call log       model, latency, cost, blocked |
    +--------------------------------------------------+

Why: if the main model is down, busy or too slow, users still get an answer from a
backup model, and the security rules live in one place instead of in every app.

All models here are local (Ollama). In a real company the chain could be
"OpenAI -> Anthropic -> a local model"; only MODEL_CHAIN changes.
"""

import os
import time

import litellm
from langchain_core.messages import AIMessage
from litellm import Router

from security import check_input, check_output

litellm.suppress_debug_info = True

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEAD_URL = "http://localhost:11999"  # nothing listens here: used to simulate an outage
MODEL_CHAIN = ["qwen2.5:7b", "gemma2:2b", "llama3.2:1b"]  # primary first, then backups
REFUSAL = "Sorry, I can only help with questions about Acme Cloud."


def build_router(primary_down: bool = False, primary_timeout: float = 120, fallbacks: bool = True) -> Router:
    """One deployment per model. If the primary fails, the router tries the next one.

    primary_down     simulate an outage (the primary points to a port with no server)
    primary_timeout  seconds before a slow primary counts as failed ("too busy")
    fallbacks        False = a plain single-model setup, to compare against
    """
    model_list = []
    for i, name in enumerate(MODEL_CHAIN):
        is_primary = i == 0
        model_list.append({
            "model_name": name,
            "litellm_params": {
                "model": f"ollama_chat/{name}",
                "api_base": DEAD_URL if (is_primary and primary_down) else OLLAMA_URL,
                "timeout": primary_timeout if is_primary else 180,
            },
        })
    chain = [{MODEL_CHAIN[0]: MODEL_CHAIN[1:]}] if fallbacks else []
    return Router(
        model_list=model_list,
        fallbacks=chain,
        num_retries=0,      # go straight to the backup instead of retrying a broken model
        allowed_fails=1,    # after a failure, skip the broken model for a while ("cooldown")
        cooldown_time=60,
    )


def to_openai_messages(messages) -> list[dict]:
    """Accept a plain string, LangChain-style tuples, or OpenAI-style dicts."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return [m if isinstance(m, dict) else {"role": m[0], "content": m[1]} for m in messages]


class Gateway:
    """A tiny chat model: `.invoke(messages)` returns a message with `.content`,
    so it plugs into rag_core.py and secure_rag.py like any LangChain model."""

    def __init__(self, router: Router | None = None, guardrails: bool = False, cache: bool = False):
        self.router = router or build_router()
        self.guardrails = guardrails
        self.cache = cache
        self.log: list[dict] = []    # one entry per request: the observability part
        self.saved: dict[str, str] = {}  # the cache. In production: Redis, shared by all servers

    def _call_llm(self, messages: list[dict], entry: dict) -> str:
        key = str(messages)
        if self.cache and key in self.saved:  # same prompt as before: no LLM call at all
            entry["model"] = "cache"
            return self.saved[key]
        response = self.router.completion(model=MODEL_CHAIN[0], messages=messages, temperature=0)
        entry["model"] = response.model
        # LiteLLM knows the price of each provider. Local Ollama models are free: $0.00.
        entry["cost_usd"] = response._hidden_params.get("response_cost") or 0.0
        text = response.choices[0].message.content
        if self.cache:
            self.saved[key] = text
        return text

    def invoke(self, messages) -> AIMessage:
        messages = to_openai_messages(messages)
        start = time.perf_counter()
        entry = {"model": "", "blocked_by": "", "error": "", "cost_usd": 0.0}
        try:
            if self.guardrails:
                question = messages[-1]["content"]
                entry["blocked_by"] = check_input(question, judge_llm=Gateway(self.router)) or ""
            if entry["blocked_by"]:
                text = REFUSAL
            else:
                text = self._call_llm(messages, entry)
                if self.guardrails:
                    text, reason = check_output(text)
                    entry["blocked_by"] = reason or ""
        except Exception as error:  # every model in the chain failed
            entry["error"] = type(error).__name__
            raise
        finally:
            entry["seconds"] = round(time.perf_counter() - start, 2)
            self.log.append(entry)
        return AIMessage(content=text, response_metadata=entry)
