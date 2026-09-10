"""Explicit registry for actions using the typed authoring contract."""

from frisket.actions.research_types import WebSearcher
from frisket.actions.core import (
    ActionNamespace,
    ActionRegistry,
    CreateSheet,
    GoogleSheetsExport,
    SemanticJoin,
    _ProjectAction,
)
from frisket.actions.transcript_segments import TRANSCRIPT_SEGMENTS
from frisket.actions.temporal_segments import TEMPORAL_SEGMENTS
from frisket.actions.temporal_extract import EXTRACT_RANGE
from frisket.actions.mutations import (
    CELL_EDIT,
    CELL_EDIT_QUERY,
    COLUMN_ADD,
    COLUMN_PATCH,
    COLUMN_SET_TYPE,
    ROW_ADD,
    ROW_DELETE,
)
from frisket.actions.cleanup import CLEAN_COLUMN
from frisket.actions.cluster import CLUSTER_VALUES
from frisket.actions.entities import RESOLVE_ENTITIES
from frisket.actions.embeddings import (
    INDEX_CLUSTER,
    INDEX_EXPORT,
    INDEX_PROJECT,
    INDEX_UPDATE_POLICY,
    INDEX_DELETE,
    INDEX_CREATE,
    INDEX_REFRESH,
)
from frisket.actions.extract import COLUMNS_FROM_JSON, REGEX_EXTRACT, EXTRACT
from frisket.actions.exports import (
    COLUMN_TABLES,
    GOOGLE_SHEETS,
    SHEET_CSV,
    SHEET_JSONL,
    SHEET_PARQUET,
    WORK_LOG,
)
from frisket.actions.imports import (
    APPEND_CSV,
    APPEND_ROWS,
    UPDATE_ROWS,
    UPDATE_CSV,
    CSV,
    NDJSON,
    ROWS as IMPORT_ROWS,
)
from frisket.actions.import_geo import GEOJSON, KML
from frisket.actions.import_media import FILES, PDF
from frisket.actions.import_email import EMAIL
from frisket.actions.import_runtime import RUNTIME
from frisket.actions.import_urls import URLS
from frisket.actions.import_xlsx import APPEND_XLSX, UPDATE_XLSX, XLSX
from frisket.actions.list_table import TABLE_FROM_LIST
from frisket.actions.collection_expand import COLLECTION_EXPAND
from frisket.actions.link_table import LINK_TABLE
from frisket.actions.joins import JOIN
from frisket.actions.semantic_join import SEMANTIC_JOIN
from frisket.actions.media_metadata import EXTRACT_METADATA
from frisket.actions.pdf_tables import EXTRACT_PDF_TABLES
from frisket.actions.markdown import TO_MARKDOWN
from frisket.actions.media import OCR, TRANSCRIBE
from frisket.actions.enclosures import MATERIALIZE
from frisket.actions.file_fetch import FETCH_URL
from frisket.actions.media_download import YTDLP_DOWNLOAD
from frisket.actions.row_media import VIDEO_FRAMES, EXTRACT_FACES
from frisket.actions.screenshot import CAPTURE_SCREENSHOT
from frisket.actions.page_capture import CAPTURE_PAGE
from frisket.actions.geo import TO_GEO_POINT
from frisket.actions.geocode import GEOCODE
from frisket.actions.census import CENSUS_DEMOGRAPHICS
from frisket.actions.judge import JUDGE
from frisket.actions.model_rows import ASK, SUMMARIZE
from frisket.actions.classify import CLASSIFY
from frisket.actions.ner import NER
from frisket.actions.translate import TRANSLATE
from frisket.actions.mcp_extract import MCP_EXTRACT
from frisket.actions.row_research import ANSWER
from frisket.actions.group_summary import GROUP_SUMMARY
from frisket.actions.find import FIND
from frisket.actions.operations import REDO, UNDO
from frisket.actions.plugins import LOAD
from frisket.actions.query import PREVIEW
from frisket.actions.review_replay import (
    REPLAY_ACCEPT,
    REPLAY_ACCEPT_COLUMN,
    REPLAY_DISMISS,
    REVIEW_DECISION,
)
from frisket.actions.research import WEB_SEARCH
from frisket.actions.resolve import COMBINE, FILL_MISSING, REPLACE, SUBSTITUTE
from frisket.actions.sources import CHECK, CREATE, DELETE, UPDATE, POLL
from frisket.actions.sheets import REFRESH
from frisket.actions.runs import BACKFILL
from frisket.actions.text import CLEAN_DATES, TEMPLATE
from frisket.actions.temporal_finders import FIND_TOPIC_SECTIONS, FIND_VISUAL_CUTS
from frisket.actions.python import PYTHON
from frisket.actions.api_call import API_CALL


MAP_ACTIONS = ActionNamespace(
    "map",
    actions=(
        TEMPLATE,
        CLEAN_DATES,
        TO_GEO_POINT,
        REGEX_EXTRACT,
        COLUMNS_FROM_JSON,
        CLEAN_COLUMN,
        ASK,
        SUMMARIZE,
        CLASSIFY,
        NER,
        TRANSLATE,
        MCP_EXTRACT,
        EXTRACT,
        FIND,
        JUDGE,
        FIND_VISUAL_CUTS,
        FIND_TOPIC_SECTIONS,
        PYTHON,
        API_CALL,
    ),
)
RESOLVE_ACTIONS = ActionNamespace(
    "resolve",
    actions=(
        RESOLVE_ENTITIES,
        SUBSTITUTE,
        REPLACE,
        COMBINE,
        FILL_MISSING,
    ),
)
SOURCE_ACTIONS = ActionNamespace(
    "source", actions=(CREATE, UPDATE, DELETE, CHECK, POLL)
)
RUN_ACTIONS = ActionNamespace("run", actions=(BACKFILL,))
SHEET_ACTIONS = ActionNamespace("sheet", actions=(REFRESH,))
PLUGIN_ACTIONS = ActionNamespace("plugin", actions=(LOAD,))
COLUMN_ACTIONS = ActionNamespace(
    "column", actions=(COLUMN_PATCH, COLUMN_ADD, COLUMN_SET_TYPE)
)
CELL_ACTIONS = ActionNamespace("cell", actions=(CELL_EDIT, CELL_EDIT_QUERY))
ROW_ACTIONS = ActionNamespace("row", actions=(ROW_ADD, ROW_DELETE))
OPERATION_ACTIONS = ActionNamespace("operation", actions=(UNDO, REDO))
REVIEW_ACTIONS = ActionNamespace("review", actions=(REVIEW_DECISION,))
REPLAY_ACTIONS = ActionNamespace(
    "replay", actions=(REPLAY_ACCEPT, REPLAY_ACCEPT_COLUMN, REPLAY_DISMISS)
)
RESEARCH_ACTIONS = ActionNamespace("research", actions=(WEB_SEARCH, ANSWER))
REDUCE_ACTIONS = ActionNamespace("reduce", actions=(GROUP_SUMMARY,))
QUERY_ACTIONS = ActionNamespace("query", actions=(PREVIEW,))
EXPORT_ACTIONS = ActionNamespace(
    "export",
    actions=(
        WORK_LOG,
        SHEET_CSV,
        SHEET_JSONL,
        SHEET_PARQUET,
        COLUMN_TABLES,
        GOOGLE_SHEETS,
    ),
)
IMPORT_ACTIONS = ActionNamespace(
    "import",
    actions=(
        IMPORT_ROWS,
        APPEND_ROWS,
        UPDATE_ROWS,
        UPDATE_CSV,
        APPEND_CSV,
        APPEND_XLSX,
        UPDATE_XLSX,
        NDJSON,
        CSV,
        XLSX,
        GEOJSON,
        KML,
        FILES,
        PDF,
        EMAIL,
        RUNTIME,
        URLS,
    ),
)
DERIVE_ACTIONS = ActionNamespace(
    "derive",
    actions=(
        TABLE_FROM_LIST,
        COLLECTION_EXPAND,
        LINK_TABLE,
        JOIN,
        TRANSCRIPT_SEGMENTS,
        TEMPORAL_SEGMENTS,
    ),
)
MEDIA_ACTIONS = ActionNamespace(
    "media",
    actions=(
        EXTRACT_METADATA,
        EXTRACT_PDF_TABLES,
        TO_MARKDOWN,
        OCR,
        TRANSCRIBE,
        MATERIALIZE,
        FETCH_URL,
        YTDLP_DOWNLOAD,
        VIDEO_FRAMES,
        EXTRACT_FACES,
    ),
)
JOIN_ACTIONS = ActionNamespace("join", actions=(SEMANTIC_JOIN,))
CLUSTER_ACTIONS = ActionNamespace("cluster", actions=(CLUSTER_VALUES,))
WEB_ACTIONS = ActionNamespace("web", actions=(CAPTURE_SCREENSHOT, CAPTURE_PAGE))
TEMPORAL_ACTIONS = ActionNamespace("temporal", actions=(EXTRACT_RANGE,))
ENRICH_ACTIONS = ActionNamespace("enrich", actions=(GEOCODE, CENSUS_DEMOGRAPHICS))
EMBEDDING_ACTIONS = ActionNamespace(
    "embedding",
    actions=(
        INDEX_UPDATE_POLICY,
        INDEX_PROJECT,
        INDEX_CLUSTER,
        INDEX_EXPORT,
        INDEX_DELETE,
        INDEX_CREATE,
        INDEX_REFRESH,
    ),
)
ACTION_REGISTRY = ActionRegistry(
    (
        MAP_ACTIONS,
        RESOLVE_ACTIONS,
        SOURCE_ACTIONS,
        PLUGIN_ACTIONS,
        SHEET_ACTIONS,
        RUN_ACTIONS,
        COLUMN_ACTIONS,
        CELL_ACTIONS,
        ROW_ACTIONS,
        OPERATION_ACTIONS,
        REVIEW_ACTIONS,
        REPLAY_ACTIONS,
        RESEARCH_ACTIONS,
        REDUCE_ACTIONS,
        QUERY_ACTIONS,
        EXPORT_ACTIONS,
        IMPORT_ACTIONS,
        DERIVE_ACTIONS,
        JOIN_ACTIONS,
        CLUSTER_ACTIONS,
        MEDIA_ACTIONS,
        WEB_ACTIONS,
        TEMPORAL_ACTIONS,
        ENRICH_ACTIONS,
        EMBEDDING_ACTIONS,
    )
)
NEW_ACTION_IDS = frozenset(ACTION_REGISTRY.action_ids)
COPILOT_ACTION_IDS = frozenset(
    registered.action_id
    for registered in ACTION_REGISTRY.actions
    if registered.action_id
    in {
        "derive.table_from_list",
        "derive.collection_expand",
        "derive.link_table",
        "reduce.group_summary",
        "map.find",
    }
    or (
        WebSearcher not in getattr(registered.definition.run, "capabilities", ())
        and not isinstance(
            registered.definition.run,
            (_ProjectAction, GoogleSheetsExport, CreateSheet, SemanticJoin),
        )
    )
)
