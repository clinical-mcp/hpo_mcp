"""
Direct-call HPO functions for non-MCP environments.

This module intentionally has no MCP dependency. It is suitable for:
- normal Python imports,
- Snowflake Python UDF handlers via staged imports,
- model/tool runtimes that call Python functions directly.

The functions return JSON strings to match the MCP tool behavior and to make
Snowflake SQL usage straightforward.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


_ROMAN_TYPE_MAP = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
STOPWORDS = {"of", "the", "and", "or", "to", "due", "with", "without", "in", "on", "a", "an", "for", "from", "border"}
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
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = s.strip().lower()
    s = re.sub(r"[/,_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    def _type_roman_to_num(match: re.Match[str]) -> str:
        roman = match.group(1).lower()
        return "type " + _ROMAN_TYPE_MAP.get(roman, roman)

    s = re.sub(r"\btype\s+(i|ii|iii|iv|v|vi)\b", _type_roman_to_num, s)
    s = re.sub(r"\bmalformation\s+type\s+(\d+)\b", r"type \1 malformation", s)
    s = re.sub(r"\bchiari\s+malformation\s+type\s+(\d+)\b", r"chiari type \1 malformation", s)
    return re.sub(r"[;:,\.\s]+$", "", s).strip()


def bow_key(s: str) -> str:
    toks = [TOKEN_CANON.get(t, t) for t in normalize_text(s).split() if t not in STOPWORDS]
    return " ".join(sorted(set(toks))) if toks else ""


def _normalize_hpo_id(value: str) -> str:
    raw = (value or "").strip()
    match = re.search(r"HP[:_](\d{7})", raw, flags=re.IGNORECASE)
    if match:
        return f"HP:{match.group(1)}"
    return raw.upper().replace("HP_", "HP:")


def _extract_synonyms(term: Dict[str, Any]) -> List[str]:
    meta = term.get("meta", {}) or {}
    synonyms_list = meta.get("synonyms", []) or []
    out: List[str] = []
    for syn in synonyms_list:
        if isinstance(syn, dict):
            val = (syn.get("val") or "").strip()
        else:
            val = str(syn).strip()
        if val:
            out.append(val)
    return out


def _extract_definition(term: Dict[str, Any]) -> str:
    meta = term.get("meta", {}) or {}
    definition = meta.get("definition") or term.get("definition") or term.get("def") or ""
    if isinstance(definition, dict):
        definition = definition.get("val") or definition.get("text") or ""
    elif isinstance(definition, list):
        definition = " ".join(str(x) for x in definition if x)
    return str(definition or "").strip()


def _term_id(term: Dict[str, Any]) -> str:
    return _normalize_hpo_id(term.get("id") or term.get("hpo_id") or "")


def _term_name(term: Dict[str, Any]) -> str:
    return (term.get("lbl") or term.get("name") or "").strip()


def _vector_tokens(text: str) -> List[str]:
    norm = normalize_text(text)
    words = [TOKEN_CANON.get(t, t) for t in norm.split() if t and t not in STOPWORDS]
    tokens: List[str] = [f"w:{w}" for w in words]
    tokens.extend(f"b:{words[i]} {words[i + 1]}" for i in range(len(words) - 1))
    for word in re.sub(r"[^a-z0-9]+", " ", norm).split():
        canonical = TOKEN_CANON.get(word, word)
        if len(canonical) >= 4:
            padded = f" {canonical} "
            tokens.extend(f"c:{padded[i:i + 3]}" for i in range(len(padded) - 2))
    return tokens


def _token_overlap(a: str, b: str) -> float:
    a_toks, b_toks = set(a.split()), set(b.split())
    return len(a_toks & b_toks) / len(a_toks | b_toks) if a_toks and b_toks else 0.0


class HPOService:
    """In-memory HPO lookup service with lexical + local vector search."""

    def __init__(self, hp_json_path: str | Path):
        self.path = Path(hp_json_path)
        self.data: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, Any]] = []
        self.search_index: List[Dict[str, Any]] = []
        self.inverted_index: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
        self.id_index: Dict[str, Dict[str, Any]] = {}
        self.parent_index: Dict[str, Set[str]] = defaultdict(set)
        self.child_index: Dict[str, Set[str]] = defaultdict(set)
        self.idf: Dict[str, float] = {}
        self.load()

    def load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if isinstance(payload, dict) and "graphs" in payload:
            graph = payload["graphs"][0] or {}
            self.data = graph.get("nodes", []) or []
            self.edges = graph.get("edges", []) or []
        elif isinstance(payload, list):
            self.data = payload
            self.edges = []
        else:
            self.data = [payload]
            self.edges = []
        self._build_indexes()

    def _term_summary(self, hpo_id: str) -> Dict[str, Any]:
        norm = _normalize_hpo_id(hpo_id)
        term = self.id_index.get(norm)
        return {"id": norm, "name": _term_name(term)} if term else {"id": norm, "found": False}

    def _term_search_text(self, term: Dict[str, Any]) -> str:
        name = _term_name(term)
        syns = " ".join(_extract_synonyms(term))
        return " ".join([name, name, syns, syns, _extract_definition(term)]).strip()

    def _make_vector(self, tokens: List[str]) -> Dict[str, float]:
        counts = Counter(tokens)
        if not counts:
            return {}
        max_tf = max(counts.values())
        vec: Dict[str, float] = {}
        norm_sq = 0.0
        for token, count in counts.items():
            weight = (0.5 + 0.5 * (count / max_tf)) * self.idf.get(token, 1.0)
            vec[token] = weight
            norm_sq += weight * weight
        norm = math.sqrt(norm_sq) if norm_sq else 1.0
        return {token: weight / norm for token, weight in vec.items()}

    def _build_indexes(self) -> None:
        self.search_index = []
        self.inverted_index = defaultdict(list)
        self.id_index = {}
        self.parent_index = defaultdict(set)
        self.child_index = defaultdict(set)
        doc_freq: Counter[str] = Counter()
        docs: List[Tuple[Dict[str, Any], List[str], List[str]]] = []

        for term in self.data:
            term_id, name = _term_id(term), _term_name(term)
            if not term_id or not name:
                continue
            syns = _extract_synonyms(term)
            tokens = _vector_tokens(self._term_search_text(term))
            docs.append((term, syns, tokens))
            doc_freq.update(set(tokens))
            self.id_index[term_id] = term

        doc_count = max(len(docs), 1)
        self.idf = {token: math.log((1 + doc_count) / (1 + freq)) + 1.0 for token, freq in doc_freq.items()}

        for term, syns, tokens in docs:
            idx = len(self.search_index)
            vec = self._make_vector(tokens)
            entry = {
                "id": _term_id(term),
                "name": _term_name(term),
                "synonyms_norm": [normalize_text(s) for s in syns],
                "synonyms_bow": [bow_key(s) for s in syns],
                "name_norm": normalize_text(_term_name(term)),
                "name_bow": bow_key(_term_name(term)),
                "vector": vec,
            }
            self.search_index.append(entry)
            for token, weight in vec.items():
                self.inverted_index[token].append((idx, weight))

        for edge in self.edges:
            pred = str(edge.get("pred") or edge.get("predicate") or "").lower()
            if pred and not (pred.endswith("is_a") or "subclassof" in pred or pred == "is_a"):
                continue
            child = _normalize_hpo_id(str(edge.get("sub") or edge.get("subject") or edge.get("source") or ""))
            parent = _normalize_hpo_id(str(edge.get("obj") or edge.get("object") or edge.get("target") or ""))
            if child and parent and child != parent:
                self.parent_index[child].add(parent)
                self.child_index[parent].add(child)

    def _vector_candidates(self, query: str) -> Dict[int, float]:
        q_vec = self._make_vector(_vector_tokens(query))
        scores: Dict[int, float] = defaultdict(float)
        for token, query_weight in q_vec.items():
            for idx, doc_weight in self.inverted_index.get(token, []):
                scores[idx] += query_weight * doc_weight
        return dict(scores)

    def search_hpo_terms(self, query: str, limit: int = 15, vector_min_score: float = 0.08) -> str:
        q_raw = (query or "").strip()
        if not q_raw:
            return "[]"
        q_norm, q_bow = normalize_text(q_raw), bow_key(q_raw)
        vector_scores = self._vector_candidates(q_raw)
        matches: List[Dict[str, Any]] = []
        for idx, entry in enumerate(self.search_index):
            score, sim, matched_on = 9999, 0.0, ""
            term_id_low = entry["id"].lower()
            if q_raw.lower() == term_id_low or q_norm == term_id_low:
                score, sim, matched_on = 1, 1.0, "id"
            elif q_norm and q_norm == entry["name_norm"]:
                score, sim, matched_on = 2, 1.0, "name_exact"
            elif q_norm and q_norm in entry["synonyms_norm"]:
                score, sim, matched_on = 3, 1.0, "synonym_exact"
            elif q_bow and q_bow == entry["name_bow"]:
                score, sim, matched_on = 4, 1.0, "name_bag_of_words"
            elif q_bow and q_bow in entry["synonyms_bow"]:
                score, sim, matched_on = 5, 1.0, "synonym_bag_of_words"
            elif q_norm and entry["name_norm"].startswith(q_norm):
                score, sim, matched_on = 6, 0.95, "name_prefix"
            elif q_norm and (q_norm in entry["name_norm"] or q_norm in term_id_low):
                score, sim, matched_on = 7, 0.9, "substring"
            elif q_bow:
                overlap = max([_token_overlap(q_bow, entry["name_bow"])] + [_token_overlap(q_bow, x) for x in entry["synonyms_bow"]])
                if overlap >= 0.5:
                    score, sim, matched_on = 8, overlap, "token_overlap"

            vector_score = vector_scores.get(idx, 0.0)
            if score == 9999 and vector_score >= vector_min_score:
                score, sim, matched_on = 9, vector_score, "vector"
            elif vector_score >= vector_min_score:
                sim = max(sim, vector_score)
                matched_on = f"{matched_on}+vector"
            if score < 9999:
                matches.append({"id": entry["id"], "name": entry["name"], "score": score, "similarity": round(sim, 4), "vector_score": round(vector_score, 4), "matched_on": matched_on})
        matches.sort(key=lambda x: (x["score"], -x["similarity"], -x["vector_score"], len(x["name"])))
        return json.dumps(matches[:limit], indent=2)

    def get_hpo_term_details(self, hpo_id: str) -> str:
        term = self.id_index.get(_normalize_hpo_id(hpo_id))
        return json.dumps(term, indent=2) if term else f"No term found with ID: {hpo_id}"

    def get_hpo_parents(self, hpo_id: str) -> str:
        target = _normalize_hpo_id(hpo_id)
        return json.dumps({"id": target, "parents": [self._term_summary(x) for x in sorted(self.parent_index.get(target, set()))]}, indent=2)

    def get_hpo_children(self, hpo_id: str) -> str:
        target = _normalize_hpo_id(hpo_id)
        return json.dumps({"id": target, "children": [self._term_summary(x) for x in sorted(self.child_index.get(target, set()))]}, indent=2)


_SERVICE: HPOService | None = None


def get_service(hp_json_path: str | None = None) -> HPOService:
    global _SERVICE
    path = hp_json_path or os.environ.get("HPO_JSON_PATH") or str(Path(__file__).resolve().with_name("hp.json"))
    if _SERVICE is None or str(_SERVICE.path) != str(Path(path)):
        _SERVICE = HPOService(path)
    return _SERVICE


def search_hpo_terms(query: str, limit: int = 15, hp_json_path: str | None = None) -> str:
    return get_service(hp_json_path).search_hpo_terms(query, limit)


def get_hpo_term_details(hpo_id: str, hp_json_path: str | None = None) -> str:
    return get_service(hp_json_path).get_hpo_term_details(hpo_id)


def get_hpo_parents(hpo_id: str, hp_json_path: str | None = None) -> str:
    return get_service(hp_json_path).get_hpo_parents(hpo_id)


def get_hpo_children(hpo_id: str, hp_json_path: str | None = None) -> str:
    return get_service(hp_json_path).get_hpo_children(hpo_id)
