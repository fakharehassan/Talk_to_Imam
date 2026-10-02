"""
Retriever + grounded generation chain, LangChain version.

Same grounding guardrail as the raw-API version: the model is given ONLY
the retrieved candidates and must choose one explicitly by number — never
free-generate advice disconnected from an actual retrieved saying. The
chosen index is validated before use; an out-of-range or missing index
falls back to the top-retrieved candidate rather than crashing or
fabricating a citation.
"""

import os
import json
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

load_dotenv()

PERSIST_PATH = "./chroma_langchain_db"
COLLECTION_NAME = "imam_ali_sayings"


def get_vectorstore() -> Chroma:
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=embeddings,
        persist_directory=PERSIST_PATH,
        collection_metadata={"hnsw:space": "cosine"},
    )


def retrieve_candidates(vectorstore: Chroma, question: str, top_k: int = 3) -> list[dict]:
    # IMPORTANT: despite the method name, this returns DISTANCE, not similarity
    # (confirmed from langchain-chroma's own docstring: "Lower score represents
    # more similarity") — same distance-vs-similarity confusion as the raw
    # ChromaDB version earlier in this project. With cosine space configured,
    # similarity = 1 - distance, same conversion as before.
    results = vectorstore.similarity_search_with_score(question, k=top_k)
    candidates = []
    for doc, distance in results:
        candidates.append({
            "text": doc.page_content,
            "source_book": doc.metadata["source_book"],
            "saying_number": doc.metadata["saying_number"],
            "topic": doc.metadata.get("topic") or None,
            "similarity": 1 - distance,
        })
    return candidates


def build_prompt(question: str, candidates: list[dict]) -> str:
    candidates_text = "\n\n".join(
        f"[Candidate {i+1}] ({c['source_book']} #{c['saying_number']}"
        f"{', topic: ' + c['topic'] if c['topic'] else ''}): \"{c['text']}\""
        for i, c in enumerate(candidates)
    )
    return f"""A person shares this situation or question: "{question}"

Here are {len(candidates)} candidate sayings retrieved from classical Islamic wisdom texts (Ghurar al-Hikam and Nahj ul-Balagha), ranked by semantic similarity to their question:

{candidates_text}

Choose the ONE candidate that best resonates with their situation. Respond with ONLY valid JSON, no markdown fences:
{{"chosen_candidate_number": <1, 2, or 3 - MUST match one of the candidates above>, "advice": "<2-4 sentences of advice grounded specifically in that saying, explaining how it applies to their situation. Do not introduce any saying, quote, or fact not present in the candidates above.>"}}"""


def extract_text_from_content(content) -> str:
    """
    ChatGoogleGenerativeAI's response.content isn't always a plain string —
    it can be a list of content parts (e.g. [{"type": "text", "text": "..."}]
    or just a list of strings), depending on the response shape. Handle both
    rather than assuming .content is always directly string-like.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(part["text"])
        return "".join(parts)
    raise TypeError(f"Unexpected response content type: {type(content)}")


def parse_llm_response(raw_text: str, num_candidates: int) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json", "", 1).strip()
    result = json.loads(text)

    chosen_idx = result["chosen_candidate_number"] - 1
    if not (0 <= chosen_idx < num_candidates):
        chosen_idx = 0  # defensive fallback — never trust an out-of-range index blindly

    return {"chosen_idx": chosen_idx, "advice": result["advice"]}


def generate_grounded_advice(question: str, candidates: list[dict], llm: ChatGoogleGenerativeAI) -> dict:
    prompt = build_prompt(question, candidates)
    response = llm.invoke(prompt)
    response_text = extract_text_from_content(response.content)
    parsed = parse_llm_response(response_text, len(candidates))

    chosen = candidates[parsed["chosen_idx"]]
    return {
        "advice": parsed["advice"],
        "cited_saying": chosen["text"],
        "source_book": chosen["source_book"],
        "saying_number": chosen["saying_number"],
        "topic": chosen["topic"],
        "similarity": chosen["similarity"],
    }


def get_llm() -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite",
        google_api_key=os.environ["GEMINI_API_KEY"],
        temperature=0.3,
    )


if __name__ == "__main__":
    vectorstore = get_vectorstore()
    llm = get_llm()

    question = "I'm struggling financially, what should I do?"
    candidates = retrieve_candidates(vectorstore, question, top_k=3)

    print("Retrieved candidates:")
    for c in candidates:
        print(f"  [{c['source_book']} #{c['saying_number']}] (similarity: {c['similarity']:.3f}) {c['text']}")

    result = generate_grounded_advice(question, candidates, llm)
    print(f"\nAdvice: {result['advice']}")
    print(f"Cited: [{result['source_book']} #{result['saying_number']}] {result['cited_saying']}")