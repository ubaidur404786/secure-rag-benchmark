"""
One place to create the models, so the rest of the code does not care where they run.

    LLM_PROVIDER=ollama  (default)  free, local, private: runs on your own machine
    LLM_PROVIDER=openai             needs OPENAI_API_KEY

Change the models in .env (see example.env).
"""

import os

from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()

DEFAULTS = {
    "ollama": {"chat": "qwen2.5:7b", "embed": "nomic-embed-text"},
    "openai": {"chat": "gpt-4o-mini", "embed": "text-embedding-3-small"},
}

CHAT_MODEL = os.getenv("CHAT_MODEL", DEFAULTS[PROVIDER]["chat"])
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", DEFAULTS[PROVIDER]["embed"])


def get_chat_model(model: str | None = None):
    """Return a chat model. temperature=0 so the results can be repeated."""
    model = model or CHAT_MODEL
    if PROVIDER == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, temperature=0)
    from langchain_ollama import ChatOllama

    return ChatOllama(model=model, temperature=0)


def get_embeddings():
    """Return the embedding model that turns text into vectors for search."""
    if PROVIDER == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=EMBEDDING_MODEL)
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(model=EMBEDDING_MODEL)
