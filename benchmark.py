"""
Benchmark: the unsafe RAG app vs the secure RAG app, on the same questions and models.

    python benchmark.py                                    # default models
    python benchmark.py --models qwen2.5:7b llama3.2:1b
    python benchmark.py --plot-only                        # redraw charts from results/results.csv

Writes:
    results/results.csv   one row per question (answer, guard that fired, latency)
    results/summary.csv   one row per model and mode
    results/*.png         charts for the README
"""

import argparse
import time
from pathlib import Path

import pandas as pd

from attacks import ATTACKS, NORMAL_QUESTIONS
from llm_provider import get_chat_model, get_embeddings
from rag_core import build_rag_graph, build_vector_store
from secure_rag import REFUSAL, build_secure_rag_graph

RESULTS = Path("results")
DEFAULT_MODELS = ["qwen2.5:7b", "gemma2:2b", "llama3.2:1b"]


def is_correct(answer: str, expected) -> bool:
    expected = [expected] if isinstance(expected, str) else expected
    return answer != REFUSAL and any(e.lower() in answer.lower() for e in expected)


def rescore(df: pd.DataFrame) -> pd.DataFrame:
    """Score saved answers again with the current rules in attacks.py (no model calls)."""
    expected = {c["question"]: c["expected"] for c in NORMAL_QUESTIONS}
    markers = {c["question"]: c["markers"] for c in ATTACKS}
    df = df.copy()
    df["answer"] = df["answer"].fillna("")
    normal, attack = df.kind == "normal", df.kind == "attack"
    df.loc[normal, "correct"] = [is_correct(a, expected[q]) for q, a in zip(df[normal].question, df[normal].answer)]
    df.loc[normal, "refused"] = df[normal].answer == REFUSAL
    df.loc[attack, "attack_success"] = [
        any(m in a.lower() for m in markers[q]) for q, a in zip(df[attack].question, df[attack].answer)
    ]
    return df


def run_one(app, question: str) -> tuple[dict, float]:
    start = time.perf_counter()
    state = app.invoke({"question": question, "role": "customer"})
    return state, time.perf_counter() - start


def run_benchmark(models: list[str]) -> pd.DataFrame:
    store = build_vector_store(get_embeddings())  # embed the documents once, share them
    rows = []
    for model in models:
        llm = get_chat_model(model)
        apps = {
            "without security": build_rag_graph(llm=llm, vector_store=store),
            "with security": build_secure_rag_graph(llm=llm, vector_store=store),
        }
        for mode, app in apps.items():
            print(f"\n=== {model} | {mode} ===")
            for case in ATTACKS:
                state, seconds = run_one(app, case["question"])
                answer = state["answer"].lower()
                success = any(m in answer for m in case["markers"])
                rows.append({
                    "model": model, "mode": mode, "kind": "attack", "category": case["category"],
                    "question": case["question"], "answer": state["answer"],
                    "blocked_by": state.get("blocked_by", ""), "attack_success": success,
                    "correct": None, "seconds": round(seconds, 2),
                })
                print(f"  [{'LEAK' if success else 'safe'}] {case['question'][:70]}")
            for case in NORMAL_QUESTIONS:
                state, seconds = run_one(app, case["question"])
                refused = state["answer"] == REFUSAL
                rows.append({
                    "model": model, "mode": mode, "kind": "normal", "category": "Normal question",
                    "question": case["question"], "answer": state["answer"],
                    "blocked_by": state.get("blocked_by", ""), "attack_success": None,
                    "correct": is_correct(state["answer"], case["expected"]),
                    "refused": refused, "seconds": round(seconds, 2),
                })
            pd.DataFrame(rows).to_csv(RESULTS / "results.csv", index=False)  # save as we go
    return pd.DataFrame(rows)


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    attacks = df[df.kind == "attack"].astype({"attack_success": float})
    normal = df[df.kind == "normal"].astype({"correct": float, "refused": float})
    keys = ["model", "mode"]
    summary = pd.DataFrame({
        "attack_success_rate_%": attacks.groupby(keys).attack_success.mean() * 100,
        "normal_correct_%": normal.groupby(keys).correct.mean() * 100,
        "false_refusal_%": normal.groupby(keys).refused.mean() * 100,
        "avg_seconds": df.groupby(keys).seconds.mean(),
    }).round(1).reset_index()
    by_category = (attacks.groupby(keys + ["category"]).attack_success.mean() * 100).round(1)
    by_category.unstack("category").to_csv(RESULTS / "by_category.csv")
    summary.to_csv(RESULTS / "summary.csv", index=False)
    return summary


def plot(df: pd.DataFrame) -> None:
    import matplotlib.pyplot as plt

    colors = {"without security": "#d9534f", "with security": "#2e8b57"}
    attacks = df[df.kind == "attack"].copy()
    attacks["attack_success"] = attacks.attack_success.astype(bool)

    # Chart 1: attack success rate per model
    rate = (attacks.groupby(["model", "mode"]).attack_success.mean() * 100).unstack("mode")
    rate = rate[[c for c in colors if c in rate.columns]]
    ax = rate.plot.bar(color=[colors[c] for c in rate.columns], figsize=(8, 4.5), rot=0, width=0.7)
    ax.set_ylabel("Attacks that succeeded (%)")
    ax.set_xlabel("")
    ax.set_ylim(0, 100)
    ax.set_title("Attack success rate: lower is better")
    for container in ax.containers:
        ax.bar_label(container, fmt="%.0f%%", padding=2)
    ax.legend(title=None, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    plt.tight_layout()
    plt.savefig(RESULTS / "attack_success_by_model.png", dpi=150)
    plt.close()

    # Chart 2: attack success per category (all models together)
    cat = (attacks.groupby(["category", "mode"]).attack_success.mean() * 100).unstack("mode")
    cat = cat[[c for c in colors if c in cat.columns]]
    ax = cat.plot.barh(color=[colors[c] for c in cat.columns], figsize=(8, 4.5), width=0.7)
    ax.set_xlabel("Attacks that succeeded (%)")
    ax.set_ylabel("")
    ax.set_xlim(0, 100)
    ax.set_title("Attack success by attack type (all models)")
    for container in ax.containers:
        ax.bar_label(container, fmt="%.0f%%", padding=2)
    ax.legend(title=None, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    plt.tight_layout()
    plt.savefig(RESULTS / "attack_success_by_category.png", dpi=150)
    plt.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--plot-only", action="store_true")
    args = parser.parse_args()
    RESULTS.mkdir(exist_ok=True)

    df = pd.read_csv(RESULTS / "results.csv") if args.plot_only else run_benchmark(args.models)
    df = rescore(df)
    df.to_csv(RESULTS / "results.csv", index=False)
    print("\n", summarize(df).to_string(index=False))
    plot(df)
    print(f"\nSaved results and charts in {RESULTS}/")


if __name__ == "__main__":
    main()
