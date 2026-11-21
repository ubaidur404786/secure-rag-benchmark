"""
Benchmark: what does an LLM gateway add? Three experiments, all on local models.

    1. Reliability   the primary model is healthy / down / too slow.
                     Single model (no gateway) vs gateway with fallbacks.
    2. Cache         the same 14 questions asked twice through the gateway.
    3. Guardrails    the UNSAFE RAG app (no security of its own), called directly
                     vs called through a gateway that applies the checks for it.

    python gateway_benchmark.py

Writes results/gateway_reliability.csv, gateway_cache.csv, gateway_guardrails.csv and charts.
"""

import time
from pathlib import Path

import pandas as pd

from attacks import ATTACKS, NORMAL_QUESTIONS
from benchmark import is_correct
from gateway import Gateway, build_router
from llm_provider import get_embeddings
from rag_core import build_rag_graph, build_vector_store
from secure_rag import build_secure_rag_graph

RESULTS = Path("results")

SCENARIOS = {
    "healthy": {},
    "primary down": {"primary_down": True},
    "primary too slow (1 s limit)": {"primary_timeout": 1},
}


def ask(app, question: str) -> tuple[str, bool, float]:
    """Return (answer, ok, seconds). ok=False means the user saw an error instead of an answer."""
    start = time.perf_counter()
    try:
        state = app.invoke({"question": question, "role": "customer"})
        answer, ok = state["answer"], True
    except Exception as error:
        answer, ok = f"ERROR: {type(error).__name__}", False
    return answer, ok, round(time.perf_counter() - start, 2)


def reliability(store) -> pd.DataFrame:
    rows = []
    for scenario, settings in SCENARIOS.items():
        for setup, use_fallbacks in [("single model", False), ("gateway", True)]:
            gateway = Gateway(build_router(fallbacks=use_fallbacks, **settings))
            app = build_secure_rag_graph(llm=gateway, judge_llm=gateway, vector_store=store)
            print(f"\n=== {scenario} | {setup} ===")
            for case in NORMAL_QUESTIONS:
                before = len(gateway.log)
                answer, ok, seconds = ask(app, case["question"])
                models = {e["model"] for e in gateway.log[before:] if e["model"]}
                rows.append({
                    "scenario": scenario, "setup": setup, "question": case["question"],
                    "answer": answer, "answered": ok, "correct": ok and is_correct(answer, case["expected"]),
                    "model_used": ", ".join(sorted(models)).replace("ollama_chat/", ""), "seconds": seconds,
                    "cost_usd": sum(e["cost_usd"] for e in gateway.log[before:]),
                })
                print(f"  [{'ok ' if ok else 'ERR'}] {rows[-1]['model_used']:<14} {case['question'][:55]}")
    return pd.DataFrame(rows)


def cache(store) -> pd.DataFrame:
    gateway = Gateway(build_router(), cache=True)
    app = build_rag_graph(llm=gateway, vector_store=store)
    rows = []
    for round_name in ["1st time (LLM call)", "2nd time (cache)"]:
        for case in NORMAL_QUESTIONS:
            answer, ok, seconds = ask(app, case["question"])
            rows.append({"round": round_name, "question": case["question"], "seconds": seconds,
                         "correct": ok and is_correct(answer, case["expected"])})
    return pd.DataFrame(rows)


def guardrails(store) -> pd.DataFrame:
    rows = []
    setups = {
        "unsafe app, direct": Gateway(build_router(), guardrails=False),
        "unsafe app + gateway guardrails": Gateway(build_router(), guardrails=True),
    }
    for setup, gateway in setups.items():
        app = build_rag_graph(llm=gateway, vector_store=store)  # the app has NO security of its own
        print(f"\n=== {setup} ===")
        for case in ATTACKS:
            answer, ok, seconds = ask(app, case["question"])
            rows.append({"setup": setup, "kind": "attack", "question": case["question"], "answer": answer,
                         "attack_success": any(m in answer.lower() for m in case["markers"]), "seconds": seconds})
        for case in NORMAL_QUESTIONS:
            answer, ok, seconds = ask(app, case["question"])
            rows.append({"setup": setup, "kind": "normal", "question": case["question"], "answer": answer,
                         "correct": ok and is_correct(answer, case["expected"]), "seconds": seconds})
    return pd.DataFrame(rows)


def plot(rel: pd.DataFrame) -> None:
    import matplotlib.pyplot as plt

    colors = {"single model": "#d9534f", "gateway": "#2e8b57"}
    rate = (rel.groupby(["scenario", "setup"]).answered.mean() * 100).unstack("setup")[list(colors)]
    rate = rate.reindex(list(SCENARIOS))
    ax = rate.plot.bar(color=list(colors.values()), figsize=(8, 4.5), rot=0, width=0.7)
    ax.set_ylabel("Questions answered (%)")
    ax.set_xlabel("")
    ax.set_ylim(0, 125)
    ax.set_title("Reliability: answers delivered when the main model fails")
    for container in ax.containers:
        ax.bar_label(container, fmt="%.0f%%", padding=2)
    ax.legend(title=None, frameon=False, loc="upper center", ncol=2)
    ax.spines[["top", "right"]].set_visible(False)
    plt.tight_layout()
    plt.savefig(RESULTS / "gateway_reliability.png", dpi=150)
    plt.close()


def main() -> None:
    RESULTS.mkdir(exist_ok=True)
    store = build_vector_store(get_embeddings())

    rel = reliability(store)
    rel.to_csv(RESULTS / "gateway_reliability.csv", index=False)
    plot(rel)
    print(rel.groupby(["scenario", "setup"], sort=False).agg(
        answered_pct=("answered", lambda s: round(s.mean() * 100, 1)),
        correct_pct=("correct", lambda s: round(s.mean() * 100, 1)),
        avg_seconds=("seconds", "mean"),
        total_cost_usd=("cost_usd", "sum"),
        models=("model_used", lambda s: ", ".join(sorted(set(s) - {""}))),
    ).round(2).to_string())

    cached = cache(store)
    cached.to_csv(RESULTS / "gateway_cache.csv", index=False)
    print("\n", cached.groupby("round").agg(avg_seconds=("seconds", "mean"), correct=("correct", "mean")).round(3))

    guard = guardrails(store)
    guard.to_csv(RESULTS / "gateway_guardrails.csv", index=False)
    attacks, normal = guard[guard.kind == "attack"], guard[guard.kind == "normal"]
    print("\n", pd.DataFrame({
        "attack_success_%": attacks.groupby("setup").attack_success.mean().astype(float) * 100,
        "normal_correct_%": normal.groupby("setup").correct.mean().astype(float) * 100,
    }).round(1))


if __name__ == "__main__":
    main()
