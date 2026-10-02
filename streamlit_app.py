"""
Streamlit test harness — calls the LangChain retriever + generation chain
DIRECTLY (no FastAPI layer yet), so you can iterate fast while the chain
logic is still settling. Shows all retrieved candidates (not just the
final answer) so you can debug retrieval quality, not just final output.
"""

import streamlit as st
from rag_chain import get_vectorstore, get_llm, retrieve_candidates, generate_grounded_advice

st.set_page_config(page_title="Talk to Imam Ali", page_icon="", layout="centered")

st.title("Talk to Imam Ali")
# st.caption("Calls the LangChain retriever + generation chain directly. Shows all retrieved "
#            "candidates alongside the final grounded answer, for debugging as you build.")


@st.cache_resource
def load_resources():
    return get_vectorstore(), get_llm()


vectorstore, llm = load_resources()

question = st.text_area(
    "Your question or situation",
    placeholder="e.g. I'm struggling financially, what should I do?",
    height=100,
)

top_k = st.slider("Number of candidates to retrieve", min_value=1, max_value=10, value=3)

if st.button("Run", type="primary"):
    if not question.strip():
        st.warning("Enter a question first.")
    else:
        with st.spinner("Retrieving candidates..."):
            try:
                candidates = retrieve_candidates(vectorstore, question, top_k=top_k)
            except Exception as e:
                st.error(f"Retrieval failed: {e}")
                st.stop()

        st.subheader(f"Retrieved {len(candidates)} candidates")
        for i, c in enumerate(candidates):
            with st.expander(f"[{i+1}] {c['source_book']} #{c['saying_number']} "
                              f"(similarity: {c['similarity']:.3f})"):
                st.write(c["text"])
                if c.get("topic"):
                    st.caption(f"Topic: {c['topic']}")

        with st.spinner("Generating grounded advice..."):
            try:
                result = generate_grounded_advice(question, candidates, llm)
            except Exception as e:
                st.error(f"Generation failed: {e}")
                st.stop()

        st.divider()
        st.subheader("Generated advice")
        st.write(result["advice"])

        st.subheader("Cited saying")
        st.markdown(f"> {result['cited_saying']}")
        col1, col2, col3 = st.columns(3)
        col1.metric("Source", result["source_book"])
        col2.metric("Saying #", result["saying_number"])
        col3.metric("Similarity", f"{result['similarity']:.3f}")
        if result.get("topic"):
            st.caption(f"Topic: {result['topic']}")

st.divider()
with st.sidebar:
    st.header("Dev info")
    st.write(f"Collection size: {vectorstore._collection.count()}")
    st.caption("This app calls rag_chain.py functions directly — no FastAPI layer. "
               "Once the chain is stable, wrap it in an API for the production frontend.")