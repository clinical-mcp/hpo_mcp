"""
Model-agnostic HPO MCP server.

This script is designed to work with any LLM client that supports MCP by:
- exposing the same stable tool names/signatures,
- allowing transport selection via environment variables (stdio/sse),
- keeping behavior independent of model-specific prompt quirks.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP


# -----------------------------------------------------------------------------
# SERVER CONFIG (MODEL-AGNOSTIC)
# -----------------------------------------------------------------------------

SERVER_NAME = os.environ.get("HPO_MCP_SERVER_NAME", "HPO-MCP-Universal")
HOST = os.environ.get("HPO_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("HPO_MCP_PORT", "8000"))
TRANSPORT = os.environ.get("HPO_MCP_TRANSPORT", "sse").lower().strip()
HPO_JSON_URL = os.environ.get("HPO_JSON_URL", "http://purl.obolibrary.org/obo/hp.json").strip()


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


AUTO_DOWNLOAD = _is_truthy(os.environ.get("HPO_MCP_AUTO_DOWNLOAD", "true"))
REFRESH_ON_START = _is_truthy(os.environ.get("HPO_MCP_REFRESH_ON_START", "false"))

mcp = FastMCP(SERVER_NAME, host=HOST, port=PORT)


# -----------------------------------------------------------------------------
# DATA LOADING
# -----------------------------------------------------------------------------

HPO_DATA: List[Dict[str, Any]] = []


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


load_data()


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


def _token_overlap(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a_toks = set(a.split())
    b_toks = set(b.split())
    if not a_toks or not b_toks:
        return 0.0
    return len(a_toks & b_toks) / len(a_toks | b_toks)


# -----------------------------------------------------------------------------
# MCP TOOLS
# -----------------------------------------------------------------------------

@mcp.tool()
def search_hpo_terms(query: str) -> str:
    """
    Search HPO terms with robust ranking.

    Ranking:
      1) Exact ID match
      2) Exact normalized name
      3) Exact normalized synonym
      4) Exact bag-of-words name
      5) Exact bag-of-words synonym
      6) Starts-with normalized name
      7) Substring normalized name or id
      8) Token-overlap fallback
    Returns top 15 matches as JSON.
    """
    if not HPO_DATA:
        return "Error: Database not loaded."

    q_raw = (query or "").strip()
    q_norm = normalize_text(q_raw)
    q_bow = bow_key(q_raw)
    matches = []

    for term in HPO_DATA:
        term_id = (term.get("id") or "").strip()
        term_lbl = (term.get("lbl") or term.get("name") or "").strip()
        if not term_id or not term_lbl:
            continue

        term_id_low = term_id.lower()
        lbl_norm = normalize_text(term_lbl)
        lbl_bow = bow_key(term_lbl)

        syns = _extract_synonyms(term)
        syns_norm = [normalize_text(s) for s in syns if s]
        syns_bow = [bow_key(s) for s in syns if s]

        score = 9999
        similarity = 0.0

        if q_raw.lower() == term_id_low or q_norm == term_id_low:
            score = 1
        elif q_norm and q_norm == lbl_norm:
            score = 2
        elif q_norm and q_norm in syns_norm:
            score = 3
        elif q_bow and q_bow == lbl_bow:
            score = 4
        elif q_bow and q_bow in syns_bow:
            score = 5
        elif q_norm and lbl_norm.startswith(q_norm):
            score = 6
        elif q_norm and (q_norm in lbl_norm or q_norm in term_id_low):
            score = 7
        elif q_bow:
            best = _token_overlap(q_bow, lbl_bow)
            if syns_bow:
                best = max(best, max((_token_overlap(q_bow, sb) for sb in syns_bow), default=0.0))
            if best >= 0.50:
                score = 8
                similarity = best
            else:
                continue
        else:
            continue

        matches.append(
            {
                "id": term_id,
                "name": term_lbl,
                "score": score,
                "similarity": round(similarity, 4),
            }
        )

    matches.sort(key=lambda x: (x["score"], -x.get("similarity", 0.0), len(x["name"])))
    return json.dumps(matches[:15], indent=2)


@mcp.tool()
def get_hpo_term_details(hpo_id: str) -> str:
    """Retrieve full details for a specific HPO ID (e.g., HP:0001250)."""
    target = (hpo_id or "").strip().upper().replace("HP_", "HP:")
    for term in HPO_DATA:
        tid = (term.get("id") or "").strip().upper().replace("HP_", "HP:")
        if tid == target:
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
