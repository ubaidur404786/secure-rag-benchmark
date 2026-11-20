"""
The SAME RAG app as rag_core.py, with the four security layers switched on.
This is the "after" in the comparison.

    question -> input_guard --blocked--> refuse
                    |
                    v
             retrieve (only docs the user's role may read)
                    |
                    v
             clean_context (drop poisoned chunks) -> generate -> output_guard -> answer
"""

from langgraph.graph import END, START, StateGraph

from llm_provider import get_chat_model
from rag_core import SYSTEM_PROMPT, TOP_K, RAGState, build_vector_store
from security import ISOLATION_RULES, allowed_for, check_input, check_output, clean_context, wrap_documents

REFUSAL = "Sorry, I can only help with questions about Acme Cloud."


def build_secure_rag_graph(llm=None, judge_llm=None, embeddings=None, vector_store=None, use_llm_judge=True):
    llm = llm or get_chat_model()
    if use_llm_judge:
        judge_llm = judge_llm or llm
    else:
        judge_llm = None
    vector_store = vector_store or build_vector_store(embeddings)

    def input_guard(state: RAGState) -> dict:
        reason = check_input(state["question"], judge_llm)
        if reason:
            return {"blocked_by": reason, "answer": REFUSAL, "context": []}
        return {"blocked_by": ""}

    def retrieve(state: RAGState) -> dict:
        role_filter = allowed_for(state.get("role", "customer"))
        docs = vector_store.similarity_search(state["question"], k=TOP_K, filter=role_filter)
        return {"context": clean_context(docs)}

    def generate(state: RAGState) -> dict:
        messages = [
            ("system", f"{SYSTEM_PROMPT}\n{ISOLATION_RULES}\n\nContext:\n{wrap_documents(state['context'])}"),
            ("user", state["question"]),
        ]
        return {"answer": llm.invoke(messages).content}

    def output_guard(state: RAGState) -> dict:
        answer, reason = check_output(state["answer"])
        return {"answer": answer, "blocked_by": reason or ""}

    def route_after_input(state: RAGState) -> str:
        return "refuse" if state["blocked_by"] else "retrieve"

    graph = StateGraph(RAGState)
    graph.add_node("input_guard", input_guard)
    graph.add_node("retrieve", retrieve)
    graph.add_node("generate", generate)
    graph.add_node("output_guard", output_guard)
    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", route_after_input, {"refuse": END, "retrieve": "retrieve"})
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "output_guard")
    graph.add_edge("output_guard", END)
    return graph.compile()
