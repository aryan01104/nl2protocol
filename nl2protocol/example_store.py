"""
example_store.py

RAG-based protocol example retrieval.
Loads (intent, schema) pairs from examples/ and retrieves similar ones
as few-shot context for the LLM.

Uses:
- sentence-transformers for embeddings
- chromadb for local vector storage
"""

import json
import os
import logging
import warnings
from pathlib import Path
from typing import List, Dict, Any, Optional


def _get_default_examples_dir() -> Path:
    """Get the default examples directory (inside the package)."""
    return Path(__file__).parent / "examples"


def _suppress_verbose_logging():
    """Suppress verbose output from sentence-transformers and related libs."""
    # Suppress sentence-transformers and transformers logging
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    logging.getLogger("transformers").setLevel(logging.ERROR)
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
    logging.getLogger("filelock").setLevel(logging.ERROR)

    # Suppress chromadb logging
    logging.getLogger("chromadb").setLevel(logging.ERROR)

    # Suppress tokenizers parallelism warning
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    # Suppress tqdm progress bars and HF verbosity
    os.environ["TRANSFORMERS_VERBOSITY"] = "error"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["TQDM_DISABLE"] = "1"

    # Suppress HF unauthenticated warning
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"

    # Suppress all warnings from these modules
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")
    warnings.filterwarnings("ignore", category=UserWarning, module="sentence_transformers")


class ExampleStore:
    """
    Semantic retrieval store for protocol examples.

    Loads examples from JSON files, embeds them, and provides
    similarity search to find relevant few-shot examples.
    """

    def __init__(
        self,
        examples_dir: str = None,
        persist_dir: str = None,
        collection_name: str = "protocols"
    ):
        # Default to package-bundled examples
        if examples_dir is None:
            self.examples_dir = _get_default_examples_dir()
        else:
            self.examples_dir = Path(examples_dir)

        # Default persist_dir to be next to examples
        if persist_dir is None:
            persist_dir = str(self.examples_dir.parent / ".chroma_db")
        self.persist_dir = persist_dir
        self.collection_name = collection_name

        self._encoder = None
        self._db = None
        self._collection = None
        self._initialized = False

    def _lazy_init(self):
        """Initialize embeddings and vector store on first use."""
        if self._initialized:
            return

        # Suppress verbose output before imports
        _suppress_verbose_logging()

        try:
            import sys
            import io

            # Capture both stdout and stderr to suppress HF warnings
            # (HF uses print() directly, not logging)
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()

            try:
                from sentence_transformers import SentenceTransformer
                import chromadb
                self._encoder = SentenceTransformer('all-MiniLM-L6-v2', device='cpu')
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr

            # Local persistent storage
            self._db = chromadb.PersistentClient(path=self.persist_dir)
            self._collection = self._db.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )

            self._initialized = True

            # Load examples from disk
            self._load_examples()

        except ImportError as e:
            raise ImportError(
                "RAG requires: pip install sentence-transformers chromadb"
            ) from e

    def _load_examples(self):
        """Load examples from examples/ directory into vector store."""
        if not self.examples_dir.exists():
            # Silently skip - RAG is optional enhancement
            return

        loaded = 0
        for file_path in self.examples_dir.glob("*.json"):
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)

                self._add_to_db(data, skip_if_exists=True)
                loaded += 1
            except Exception:
                # Silently skip bad files
                continue

    def _add_to_db(self, data: Dict, skip_if_exists: bool = False):
        """Add an example to the vector store."""
        example_id = data.get("id", str(hash(data.get("intent", ""))))
        intent = data.get("intent", "")

        if not intent:
            return

        if skip_if_exists:
            existing = self._collection.get(ids=[example_id])
            if existing and existing['ids']:
                return

        # Embed the intent
        embedding = self._encoder.encode(intent).tolist()

        self._collection.upsert(
            ids=[example_id],
            embeddings=[embedding],
            documents=[json.dumps(data)],
            metadatas=[{"intent": intent[:200]}]  # Truncate for metadata
        )

    def add(self, intent: str, output_schema: Dict, example_id: str = None, style: str = "manual"):
        """
        Add a new example to the store.

        Args:
            intent: Natural language protocol description
            output_schema: The JSON schema for this intent
            example_id: Optional ID (auto-generated if not provided)
            style: Style tag (technical, casual, concise, manual)
        """
        self._lazy_init()

        if example_id is None:
            import hashlib
            short_hash = hashlib.md5(intent.encode()).hexdigest()[:8]
            example_id = f"ex_{style}_{short_hash}"

        data = {
            "id": example_id,
            "intent": intent,
            "style": style,
            "output_schema": output_schema
        }

        self._add_to_db(data)

        # Also save to file for persistence
        self.examples_dir.mkdir(parents=True, exist_ok=True)
        with open(self.examples_dir / f"{example_id}.json", 'w') as f:
            json.dump(data, f, indent=2)

    def find_similar(self, intent: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """
        Find examples similar to the given intent.

        Args:
            intent: User's natural language protocol description
            top_k: Number of examples to return

        Returns:
            List of example dicts with intent and output_schema
        """
        self._lazy_init()

        if self._collection.count() == 0:
            return []

        embedding = self._encoder.encode(intent).tolist()

        results = self._collection.query(
            query_embeddings=[embedding],
            n_results=min(top_k, self._collection.count())
        )

        examples = []
        if results and results.get("documents"):
            for doc in results["documents"][0]:
                try:
                    examples.append(json.loads(doc))
                except Exception:
                    continue

        return examples

    def format_for_prompt(self, intent: str, top_k: int = 3) -> str:
        """
        Get similar examples formatted for inclusion in LLM prompt.

        Args:
            intent: User's protocol description
            top_k: Number of examples to include

        Returns:
            Formatted string ready for prompt injection, or empty string
        """
        examples = self.find_similar(intent, top_k)

        if not examples:
            return ""

        lines = ["\n=== SIMILAR EXAMPLES (use as reference for output format) ===\n"]

        for i, ex in enumerate(examples, 1):
            lines.append(f"--- Example {i} ---")
            lines.append(f"INTENT: {ex.get('intent', 'N/A')}")
            lines.append(f"OUTPUT:")
            lines.append(json.dumps(ex.get('output_schema', {}), indent=2))
            lines.append("")

        lines.append("=== END EXAMPLES ===\n")
        return "\n".join(lines)

    def count(self) -> int:
        """Return number of examples in store."""
        self._lazy_init()
        return self._collection.count()

    def clear(self):
        """Clear all examples from the vector store."""
        self._lazy_init()
        self._db.delete_collection(self.collection_name)
        self._collection = self._db.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"}
        )
