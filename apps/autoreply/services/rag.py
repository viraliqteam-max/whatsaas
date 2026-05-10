def get_rag_context(conversation, user_text: str, lead) -> str:
    """
    RAG extension point.

    Later this can call embeddings/vector search for FAQs, case studies, and
    sales scripts. Returning an empty string keeps the current system simple.
    """
    return ""
