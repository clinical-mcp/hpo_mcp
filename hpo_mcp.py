"""
Model-agnostic HPO MCP server.

This script is designed to work with any LLM client that supports MCP by:
- exposing the same stable tool names/signatures,
- allowing transport selection via environment variables (stdio/sse),
- keeping behavior independent of model-specific prompt quirks.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

from mcp.server.fastmcp import FastMCP


# -----------------------------------------------------------------------------
# SERVER CONFIG (MODEL-AGNOSTIC)
# -----------------------------------------------------------------------------

SERVER_NAME = os.environ.get("HPO_MCP_SERVER_NAME", "HPO-MCP-Universal")
HOST = os.environ.get("HPO_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("HPO_MCP_PORT", "8000"))
TRANSPORT = os.environ.get("HPO_MCP_TRANSPORT", "sse").lower().strip()
HPO_JSON_URL = os.environ.get("HPO_JSON_URL", "http://purl.obolibrary.org/obo/hp.json").strip()
SEARCH_MODE = os.environ.get("HPO_MCP_SEARCH_MODE", "hybrid").lower().strip()
DEFAULT_SEARCH_LIMIT = int(os.environ.get("HPO_MCP_SEARCH_LIMIT", "15"))
VECTOR_MIN_SCORE = float(os.environ.get("HPO_MCP_VECTOR_MIN_SCORE", "0.08"))
VECTOR_BACKEND = os.environ.get("HPO_MCP_VECTOR_BACKEND", "tfidf").lower().strip()
SEMANTIC_MODEL_NAME = os.environ.get("HPO_MCP_SEMANTIC_MODEL", "sentence-transformers/all-MiniLM-L6-v2").strip()


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


AUTO_DOWNLOAD = _is_truthy(os.environ.get("HPO_MCP_AUTO_DOWNLOAD", "true"))
REFRESH_ON_START = _is_truthy(os.environ.get("HPO_MCP_REFRESH_ON_START", "false"))

mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)


# -----------------------------------------------------------------------------
# DATA LOADING
# -----------------------------------------------------------------------------

HPO_DATA: List[Dict[str, Any]] = []
HPO_SEARCH_INDEX: List[Dict[str, Any]] = []
HPO_INVERTED_INDEX: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
HPO_ID_INDEX: Dict[str, Dict[str, Any]] = {}
IDF: Dict[str, float] = {}
SEMANTIC_MODEL: Any = None
SEMANTIC_EMBEDDINGS: List[List[float]] = []
SEMANTIC_BACKEND_ACTIVE = False


def _candidate_data_paths() -> List[Path]:
    """Return candidate file paths for hp.json, in priority order."""
    env_path = os.environ.get("HPO_JSON_PATH", "").strip()
    candidates: List[Path] = []

    if env_path:
        candidates.append(Path(env_path))

    # Prefer file next to this script.
    candidates.append(Path(__file__).resolve().with_name("hp.json"))

    # Fallback to current working directory.
    candidates.append(Path.cwd() / "hp.json")

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique: List[Path] = []
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def load_data() -> None:
    """Load HPO data from hp.json (supports common structures)."""
    global HPO_DATA

    def _load_from_file(filepath: Path) -> bool:
        global HPO_DATA
        try:
            print(f"Loading HPO data from {filepath}...")
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            if isinstance(data, dict) and "graphs" in data:
                # OBO JSON structure
                HPO_DATA = data["graphs"][0].get("nodes", [])
            elif isinstance(data, list):
                HPO_DATA = data
            else:
                HPO_DATA = list(data) if isinstance(data, tuple) else [data]

            print(f"SUCCESS: Loaded {len(HPO_DATA)} HPO terms.")
            return True
        except Exception as exc:
            print(f"WARN: Failed reading {filepath}: {exc}")
            return False

    def _download_hp_json(target_path: Path) -> bool:
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading hp.json from {HPO_JSON_URL} to {target_path} ...")
            with urllib.request.urlopen(HPO_JSON_URL, timeout=120) as response:
                payload = response.read()
            if not payload:
                raise RuntimeError("Downloaded file is empty.")
            with open(target_path, "wb") as f:
                f.write(payload)
            print("SUCCESS: hp.json download complete.")
            return True
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            print(f"WARN: hp.json download failed: {exc}")
            return False

    env_path = os.environ.get("HPO_JSON_PATH", "").strip()
    download_target = Path(env_path) if env_path else Path(__file__).resolve().with_name("hp.json")

    # Optional refresh mode: force download latest data on each start.
    if REFRESH_ON_START and AUTO_DOWNLOAD:
        _download_hp_json(download_target)

    # Try loading existing files first.
    for filepath in _candidate_data_paths():
        if not filepath.exists():
            continue
        if _load_from_file(filepath):
            return

    # If nothing found/loaded and auto-download is enabled, download and retry.
    if AUTO_DOWNLOAD:
        if _download_hp_json(download_target) and _load_from_file(download_target):
            return

    print(
        "ERROR: Could not load hp.json. "
        "Set HPO_JSON_PATH to a valid file, or enable download using HPO_MCP_AUTO_DOWNLOAD=true."
    )
    sys.exit(1)


# -----------------------------------------------------------------------------
# SEARCH UTILITIES
# -----------------------------------------------------------------------------

_ROMAN_TYPE_MAP = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}

STOPWORDS = {
    "of",
    "the",
    "and",
    "or",
    "to",
    "due",
    "with",
    "without",
    "in",
    "on",
    "a",
    "an",
    "for",
    "from",
    "border",
}

TOKEN_CANON = {
    "vermillion": "vermilion",
    "inequality": "discrepancy",
    "unequal": "discrepancy",
    "leg": "limb",
    "varus": "vara",
    "aversion": "anteversion",
}


def normalize_text(s: str) -> str:
    if not s:
        return ""

    s = re.sub(r"\s*\([^)]*\)", "", s)  # remove "(...)" notes
    s = s.strip().lower()
    s = re.sub(r"[/,_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    def _type_roman_to_num(match: re.Match[str]) -> str:
        roman = match.group(1).lower()
        return "type " + _ROMAN_TYPE_MAP.get(roman, roman)

    s = re.sub(r"\btype\s+(i|ii|iii|iv|v|vi)\b", _type_roman_to_num, s)
    s = re.sub(r"\bmalformation\s+type\s+(\d+)\b", r"type \1 malformation", s)
    s = re.sub(r"\bchiari\s+malformation\s+type\s+(\d+)\b", r"chiari type \1 malformation", s)
    s = re.sub(r"[;:,\.\s]+$", "", s).strip()
    return s


def bow_key(s: str) -> str:
    """Order-insensitive normalized token key."""
    s = normalize_text(s)
    if not s:
        return ""

    toks = []
    for token in s.split():
        if token in STOPWORDS:
            continue
        toks.append(TOKEN_CANON.get(token, token))

    if not toks:
        return ""
    return " ".join(sorted(set(toks)))


def _extract_synonyms(term: Dict[str, Any]) -> List[str]:
    meta = term.get("meta", {}) or {}
    synonyms_list = meta.get("synonyms", []) or []
    out: List[str] = []
    for syn in synonyms_list:
        if isinstance(syn, dict):
            val = (syn.get("val") or "").strip()
            if val:
                out.append(val)
        elif isinstance(syn, str):
            val = syn.strip()
            if val:
                out.append(val)
    return out


def _extract_definition(term: Dict[str, Any]) -> str:
    """Extract a definition/comment string across common OBO JSON layouts."""
    meta = term.get("meta", {}) or {}
    definition = meta.get("definition") or term.get("definition") or term.get("def") or ""
    if isinstance(definition, dict):
        definition = definition.get("val") or definition.get("text") or ""
    elif isinstance(definition, list):
        definition = " ".join(str(x) for x in definition if x)
    comment = meta.get("comments") or term.get("comment") or ""
    if isinstance(comment, list):
        comment = " ".join(str(x) for x in comment if x)
    return " ".join(str(x).strip() for x in [definition, comment] if str(x).strip())


def _token_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a_toks = set(a.split())
    b_toks = set(b.split())
    if not a_toks or not b_toks:
        return 0.0
    return len(a_toks & b_toks) / len(a_toks | b_toks)


def _vector_tokens(text: str) -> List[str]:
    """Create normalized vector tokens for TF-IDF search.

    Uses canonical word tokens, word bigrams, and character trigrams so search is
    tolerant of reordered words, minor spelling differences, pluralization, and
    partial phenotype phrases without requiring heavyweight embedding services.
    """
    norm = normalize_text(text)
    if not norm:
        return []

    words = [TOKEN_CANON.get(t, t) for t in norm.split() if t and t not in STOPWORDS]
    tokens: List[str] = [f"w:{w}" for w in words]
    tokens.extend(f"b:{words[i]} {words[i + 1]}" for i in range(len(words) - 1))

    compact = re.sub(r"[^a-z0-9]+", " ", norm).strip()
    for word in compact.split():
        canonical = TOKEN_CANON.get(word, word)
        if len(canonical) >= 4:
            padded = f" {canonical} "
            tokens.extend(f"c:{padded[i:i + 3]}" for i in range(len(padded) - 2))
    return tokens


def _term_search_text(term: Dict[str, Any]) -> str:
    term_lbl = (term.get("lbl") or term.get("name") or "").strip()
    synonyms = _extract_synonyms(term)
    definition = _extract_definition(term)
    # Weight labels/synonyms by repeating them; definitions add useful context
    # but should not drown out the canonical phenotype wording.
    return " ".join([term_lbl, term_lbl, " ".join(synonyms), " ".join(synonyms), definition]).strip()


def _make_tfidf_vector(tokens: List[str]) -> Dict[str, float]:
    counts = Counter(tokens)
    if not counts:
        return {}

    max_tf = max(counts.values())
    vector: Dict[str, float] = {}
    norm_sq = 0.0
    for token, count in counts.items():
        tf = 0.5 + 0.5 * (count / max_tf)
        weight = tf * IDF.get(token, 1.0)
        vector[token] = weight
        norm_sq += weight * weight

    if norm_sq <= 0:
        return {}
    norm = math.sqrt(norm_sq)
    return {token: weight / norm for token, weight in vector.items()}


def build_search_index() -> None:
    """Build in-memory TF-IDF vectors and an inverted index for HPO search."""
    global HPO_SEARCH_INDEX, HPO_INVERTED_INDEX, HPO_ID_INDEX, IDF

    HPO_SEARCH_INDEX = []
    HPO_INVERTED_INDEX = defaultdict(list)
    HPO_ID_INDEX = {}
    IDF = {}

    docs: List[Tuple[Dict[str, Any], List[str], List[str]]] = []
    search_texts: List[str] = []
    doc_freq: Counter[str] = Counter()

    for term in HPO_DATA:
        term_id = (term.get("id") or "").strip()
        term_lbl = (term.get("lbl") or term.get("name") or "").strip()
        if not term_id or not term_lbl:
            continue
        synonyms = _extract_synonyms(term)
        search_text = _term_search_text(term)
        tokens = _vector_tokens(search_text)
        search_texts.append(search_text)
        docs.append((term, synonyms, tokens))
        doc_freq.update(set(tokens))
        HPO_ID_INDEX[term_id.upper().replace("HP_", "HP:")] = term

    doc_count = max(len(docs), 1)
    IDF = {token: math.log((1 + doc_count) / (1 + freq)) + 1.0 for token, freq in doc_freq.items()}

    for term, synonyms, tokens in docs:
        vector = _make_tfidf_vector(tokens)
        idx = len(HPO_SEARCH_INDEX)
        entry = {
            "term": term,
            "id": (term.get("id") or "").strip(),
            "name": (term.get("lbl") or term.get("name") or "").strip(),
            "synonyms": synonyms,
            "name_norm": normalize_text(term.get("lbl") or term.get("name") or ""),
            "name_bow": bow_key(term.get("lbl") or term.get("name") or ""),
            "synonyms_norm": [normalize_text(s) for s in synonyms if s],
            "synonyms_bow": [bow_key(s) for s in synonyms if s],
            "vector": vector,
        }
        HPO_SEARCH_INDEX.append(entry)
        for token, weight in vector.items():
            HPO_INVERTED_INDEX[token].append((idx, weight))

    print(f"SUCCESS: Built HPO vector search index for {len(HPO_SEARCH_INDEX)} terms.")
    _build_semantic_embeddings(search_texts)


def _lexical_rank(entry: Dict[str, Any], q_raw: str, q_norm: str, q_bow: str) -> Tuple[int, float, str]:
    """Return (rank, similarity, matched_on), lower rank is better."""
    term_id_low = entry["id"].lower()
    if q_raw.lower() == term_id_low or q_norm == term_id_low:
        return 1, 1.0, "id"
    if q_norm and q_norm == entry["name_norm"]:
        return 2, 1.0, "name_exact"
    if q_norm and q_norm in entry["synonyms_norm"]:
        return 3, 1.0, "synonym_exact"
    if q_bow and q_bow == entry["name_bow"]:
        return 4, 1.0, "name_bag_of_words"
    if q_bow and q_bow in entry["synonyms_bow"]:
        return 5, 1.0, "synonym_bag_of_words"
    if q_norm and entry["name_norm"].startswith(q_norm):
        return 6, 0.95, "name_prefix"
    if q_norm and (q_norm in entry["name_norm"] or q_norm in term_id_low):
        return 7, 0.90, "substring"
    if q_bow:
        best = _token_overlap(q_bow, entry["name_bow"])
        if entry["synonyms_bow"]:
            best = max(best, max((_token_overlap(q_bow, sb) for sb in entry["synonyms_bow"]), default=0.0))
        if best >= 0.50:
            return 8, best, "token_overlap"
    return 9999, 0.0, ""


def _vector_candidates(query: str) -> Dict[int, float]:
    q_vec = _make_tfidf_vector(_vector_tokens(query))
    scores: Dict[int, float] = defaultdict(float)
    for token, query_weight in q_vec.items():
        for idx, doc_weight in HPO_INVERTED_INDEX.get(token, []):
            scores[idx] += query_weight * doc_weight
    return dict(scores)


def _normalize_dense_vector(values: Any) -> List[float]:
    vec = [float(x) for x in values]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return vec
    return [x / norm for x in vec]


def _build_semantic_embeddings(search_texts: List[str]) -> None:
    """Optionally build true semantic embeddings with sentence-transformers.

    This is intentionally optional so the MCP server remains lightweight by
    default. Install sentence-transformers and set HPO_MCP_VECTOR_BACKEND to
    `semantic` or `auto` to enable embedding-based semantic search.
    """
    global SEMANTIC_MODEL, SEMANTIC_EMBEDDINGS, SEMANTIC_BACKEND_ACTIVE

    SEMANTIC_MODEL = None
    SEMANTIC_EMBEDDINGS = []
    SEMANTIC_BACKEND_ACTIVE = False
    if VECTOR_BACKEND not in {"semantic", "sentence-transformers", "auto"}:
        return

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore

        print(f"Loading semantic embedding model: {SEMANTIC_MODEL_NAME} ...")
        SEMANTIC_MODEL = SentenceTransformer(SEMANTIC_MODEL_NAME)
        encoded = SEMANTIC_MODEL.encode(search_texts, show_progress_bar=False)
        SEMANTIC_EMBEDDINGS = [_normalize_dense_vector(row) for row in encoded]
        SEMANTIC_BACKEND_ACTIVE = bool(SEMANTIC_EMBEDDINGS)
        print(f"SUCCESS: Built semantic embedding index for {len(SEMANTIC_EMBEDDINGS)} terms.")
    except Exception as exc:
        SEMANTIC_MODEL = None
        SEMANTIC_EMBEDDINGS = []
        SEMANTIC_BACKEND_ACTIVE = False
        message = f"WARN: Semantic embedding backend unavailable: {exc}"
        if VECTOR_BACKEND in {"semantic", "sentence-transformers"}:
            message += " Falling back to TF-IDF vectors."
        print(message)


def _semantic_candidates(query: str) -> Dict[int, float]:
    if not SEMANTIC_BACKEND_ACTIVE or SEMANTIC_MODEL is None or not SEMANTIC_EMBEDDINGS:
        return {}
    try:
        q_vec = _normalize_dense_vector(SEMANTIC_MODEL.encode([query], show_progress_bar=False)[0])
    except Exception as exc:
        print(f"WARN: Semantic query embedding failed: {exc}")
        return {}

    scores: Dict[int, float] = {}
    for idx, doc_vec in enumerate(SEMANTIC_EMBEDDINGS):
        scores[idx] = sum(a * b for a, b in zip(q_vec, doc_vec))
    return scores


load_data()
build_search_index()


# -----------------------------------------------------------------------------
# MCP TOOLS
# -----------------------------------------------------------------------------

@mcp.tool()
def search_hpo_terms(query: str) -> str:
    """
    Search HPO terms using hybrid lexical + vector ranking.

    The default hybrid mode preserves high-precision exact/substring behavior,
    then adds vector search over names, synonyms, and definitions. By default
    vectors use local TF-IDF/ngram features; set HPO_MCP_VECTOR_BACKEND=semantic
    after installing sentence-transformers for embedding-based semantic search.
    Set HPO_MCP_SEARCH_MODE to `lexical`, `vector`, or `hybrid`.
    Returns top matches as JSON.
    """
    if not HPO_DATA or not HPO_SEARCH_INDEX:
        return "Error: Database not loaded."

    q_raw = (query or "").strip()
    if not q_raw:
        return json.dumps([], indent=2)

    q_norm = normalize_text(q_raw)
    q_bow = bow_key(q_raw)
    mode = SEARCH_MODE if SEARCH_MODE in {"hybrid", "lexical", "vector"} else "hybrid"
    vector_scores = _vector_candidates(q_raw) if mode in {"hybrid", "vector"} else {}
    semantic_scores = _semantic_candidates(q_raw) if mode in {"hybrid", "vector"} else {}
    for idx, score in semantic_scores.items():
        # Embedding cosine scores are generally larger than sparse TF-IDF scores;
        # take the strongest available vector signal for ranking.
        vector_scores[idx] = max(vector_scores.get(idx, 0.0), score)
    matches_by_id: Dict[str, Dict[str, Any]] = {}

    for idx, entry in enumerate(HPO_SEARCH_INDEX):
        lex_rank, lex_similarity, matched_on = _lexical_rank(entry, q_raw, q_norm, q_bow)
        vector_score = vector_scores.get(idx, 0.0)

        include_lexical = mode in {"hybrid", "lexical"} and lex_rank < 9999
        include_vector = mode in {"hybrid", "vector"} and vector_score >= VECTOR_MIN_SCORE
        if not include_lexical and not include_vector:
            continue

        # Lower score remains compatible with the previous API. Vector-only
        # matches start at 9 so deterministic exact matches always win.
        score = lex_rank if include_lexical else 9
        combined_similarity = max(lex_similarity, vector_score)
        if include_vector and (not matched_on or matched_on == ""):
            matched_on = "semantic_vector" if idx in semantic_scores and SEMANTIC_BACKEND_ACTIVE else "vector"
        elif include_vector and mode == "hybrid":
            matched_on = f"{matched_on}+vector"

        result = {
            "id": entry["id"],
            "name": entry["name"],
            "score": score,
            "similarity": round(combined_similarity, 4),
            "vector_score": round(vector_score, 4),
            "matched_on": matched_on,
        }
        matches_by_id[entry["id"]] = result

    matches = list(matches_by_id.values())
    matches.sort(key=lambda x: (x["score"], -x.get("similarity", 0.0), -x.get("vector_score", 0.0), len(x["name"])))
    return json.dumps(matches[:DEFAULT_SEARCH_LIMIT], indent=2)


@mcp.tool()
def get_hpo_term_details(hpo_id: str) -> str:
    """Retrieve full details for a specific HPO ID (e.g., HP:0001250)."""
    target = (hpo_id or "").strip().upper().replace("HP_", "HP:")
    term = HPO_ID_INDEX.get(target)
    if term:
        return json.dumps(term, indent=2)
    return f"No term found with ID: {hpo_id}"


if __name__ == "__main__":
    print(f"\n--- STARTING {SERVER_NAME} ---")
    print(f"Transport: {TRANSPORT}")

    if TRANSPORT == "stdio":
        print("Running in stdio mode (best for local MCP clients).")
        mcp.run(transport="stdio")
    else:
        print(f"Running in SSE mode at http://{HOST}:{PORT}/sse")
        mcp.run(transport="sse")
