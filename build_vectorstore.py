"""
Step 1 (LangChain version) — load your EXISTING, already-validated chunks
as LangChain Document objects, embed them, and store in Chroma via
LangChain's integration.

IMPORTANT: this does NOT re-chunk the PDFs. Your structure-aware chunking
(one Document = one saying, already fixed for font-encoding, appendix
contamination, footnote markers, etc.) is preserved exactly as-is — we're
only changing the embedding/storage/retrieval LAYER to use LangChain,
not redoing the hard-won parsing work.
"""

import json
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma


def load_chunks_as_documents(nahj_path: str, ghurar_path: str) -> list[Document]:
    with open(nahj_path, encoding="utf-8") as f:
        nahj_chunks = json.load(f)
    with open(ghurar_path, encoding="utf-8") as f:
        ghurar_chunks = json.load(f)

    all_chunks = nahj_chunks + ghurar_chunks
    documents = []

    for i, chunk in enumerate(all_chunks):
        # Metadata mirrors what you already validated — including the
        # topic-inclusive uniqueness lesson from the enrichment pipeline bug.
        tags = chunk.get("life_situation_tags") or []
        metadata = {
            "source_book": chunk["source_book"],
            "saying_number": chunk["saying_number"],
            "topic": chunk.get("topic") or "",
            "life_situation_tags": ", ".join(tags),  # Chroma metadata can't store lists directly
        }
        doc_id = f"{chunk['source_book']}_{chunk.get('topic') or 'none'}_{chunk['saying_number']}_{i}"

        documents.append(Document(page_content=chunk["text"], metadata=metadata, id=doc_id))

    return documents


def build_vectorstore(documents: list[Document], persist_path: str = "./chroma_langchain_db") -> Chroma:
    embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

    vectorstore = Chroma(
        collection_name="imam_ali_sayings",
        embedding_function=embeddings,
        persist_directory=persist_path,
        collection_metadata={"hnsw:space": "cosine"},  # same lesson from the raw-Chroma version — don't skip this
    )

    # Add in batches (Chroma has a max batch size, same constraint as before)
    batch_size = 500
    ids = [doc.id for doc in documents]
    for i in range(0, len(documents), batch_size):
        batch = documents[i:i + batch_size]
        batch_ids = ids[i:i + batch_size]
        vectorstore.add_documents(documents=batch, ids=batch_ids)
        print(f"  Added {min(i + batch_size, len(documents))}/{len(documents)}")

    return vectorstore


if __name__ == "__main__":
    print("Loading chunks as LangChain Documents...")
    documents = load_chunks_as_documents("nahj_chunks_step2a.json", "ghurar_enriched.json")
    print(f"Loaded {len(documents)} documents.\n")

    print("Building vector store (this embeds everything, may take a few minutes for 11,500+ chunks)...")
    vectorstore = build_vectorstore(documents)
    print(f"\nDone. Vector store built at ./chroma_langchain_db")

    # Quick test query
    results = vectorstore.similarity_search_with_score(
        "I'm struggling financially, what should I do?", k=5
    )
    print("\n=== Test query results ===")
    for doc, score in results:
        print(f"\n[{doc.metadata['source_book']} #{doc.metadata['saying_number']}] "
              f"(topic: {doc.metadata['topic'] or 'N/A'}, score: {score:.3f})")
        print(f"  {doc.page_content}")