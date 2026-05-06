-- Snowflake HPO direct-call UDF template.
--
-- 1) Upload dependencies to a named internal stage:
--      CREATE STAGE IF NOT EXISTS HPO_MCP_STAGE;
--      PUT file://hpo_functions.py @HPO_MCP_STAGE AUTO_COMPRESS=FALSE OVERWRITE=TRUE;
--      PUT file://hp.json          @HPO_MCP_STAGE AUTO_COMPRESS=FALSE OVERWRITE=TRUE;
--
-- 2) Run this SQL in the target database/schema.
--
-- Notes:
-- - These UDFs use the local TF-IDF/ngram search in hpo_functions.py.
-- - They do not require network access from Snowflake.
-- - Return type is STRING containing JSON for easy use with PARSE_JSON(...).

CREATE OR REPLACE FUNCTION HPO_SEARCH_TERMS(query STRING, limit_num INTEGER)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = 3.12
HANDLER = 'run'
IMPORTS = ('@HPO_MCP_STAGE/hpo_functions.py', '@HPO_MCP_STAGE/hp.json')
AS
$$
import os
import sys

IMPORT_DIR = sys._xoptions["snowflake_import_directory"]
sys.path.append(IMPORT_DIR)

from hpo_functions import search_hpo_terms

def run(query, limit_num):
    hp_json_path = os.path.join(IMPORT_DIR, "hp.json")
    return search_hpo_terms(query or "", int(limit_num or 15), hp_json_path)
$$;


CREATE OR REPLACE FUNCTION HPO_TERM_DETAILS(hpo_id STRING)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = 3.12
HANDLER = 'run'
IMPORTS = ('@HPO_MCP_STAGE/hpo_functions.py', '@HPO_MCP_STAGE/hp.json')
AS
$$
import os
import sys

IMPORT_DIR = sys._xoptions["snowflake_import_directory"]
sys.path.append(IMPORT_DIR)

from hpo_functions import get_hpo_term_details

def run(hpo_id):
    hp_json_path = os.path.join(IMPORT_DIR, "hp.json")
    return get_hpo_term_details(hpo_id or "", hp_json_path)
$$;


CREATE OR REPLACE FUNCTION HPO_PARENTS(hpo_id STRING)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = 3.12
HANDLER = 'run'
IMPORTS = ('@HPO_MCP_STAGE/hpo_functions.py', '@HPO_MCP_STAGE/hp.json')
AS
$$
import os
import sys

IMPORT_DIR = sys._xoptions["snowflake_import_directory"]
sys.path.append(IMPORT_DIR)

from hpo_functions import get_hpo_parents

def run(hpo_id):
    hp_json_path = os.path.join(IMPORT_DIR, "hp.json")
    return get_hpo_parents(hpo_id or "", hp_json_path)
$$;


CREATE OR REPLACE FUNCTION HPO_CHILDREN(hpo_id STRING)
RETURNS STRING
LANGUAGE PYTHON
RUNTIME_VERSION = 3.12
HANDLER = 'run'
IMPORTS = ('@HPO_MCP_STAGE/hpo_functions.py', '@HPO_MCP_STAGE/hp.json')
AS
$$
import os
import sys

IMPORT_DIR = sys._xoptions["snowflake_import_directory"]
sys.path.append(IMPORT_DIR)

from hpo_functions import get_hpo_children

def run(hpo_id):
    hp_json_path = os.path.join(IMPORT_DIR, "hp.json")
    return get_hpo_children(hpo_id or "", hp_json_path)
$$;


-- Examples:
-- SELECT PARSE_JSON(HPO_SEARCH_TERMS('seizure', 10));
-- SELECT PARSE_JSON(HPO_TERM_DETAILS('HP:0001250'));
-- SELECT PARSE_JSON(HPO_PARENTS('HP:0001250'));
-- SELECT PARSE_JSON(HPO_CHILDREN('HP:0001250'));
