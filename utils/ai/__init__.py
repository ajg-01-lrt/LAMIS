"""AI doc-assistant package for ATLAS.

Phase 1 scope: cited question-answering over ingested vendor documentation
(RAG). The model never *knows* the docs — it reads the handful of chunks the
vector index retrieves and answers strictly from them, citing the source.

Nothing here is imported unless ``config.AI_ASSISTANT_ENABLED`` is True, so the
optional dependencies (``openai``, ``pypdf``) stay optional for the rest of
ATLAS.
"""
