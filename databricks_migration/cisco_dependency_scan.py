# Databricks notebook source
# MAGIC %md
# MAGIC # Cisco WxCCE / Acqueon downstream dependency scan
# MAGIC
# MAGIC **Purpose.** Before cutting over to NICE CXone (inbound) and NICE SmartReach (outbound), find every
# MAGIC table, view, job, pipeline, notebook, dashboard, query and user that depends - directly or
# MAGIC transitively - on the Cisco WxCCE / Acqueon source schemas. Inventory only: it does not judge
# MAGIC whether a dependency has already been migrated.
# MAGIC
# MAGIC **How it works**
# MAGIC
# MAGIC | Step | What | Source |
# MAGIC |---|---|---|
# MAGIC | 1 | Seed list = every table in the Cisco / Acqueon schemas **and external (foreign) catalogs** you name | `information_schema.tables` + lineage |
# MAGIC | 2 | Walk table lineage outward from the seeds, any number of hops | `system.access.table_lineage` |
# MAGIC | 3 | Who writes each impacted table (jobs, pipelines, notebooks) | lineage + `system.lakeflow.*` |
# MAGIC | 4 | Who reads the seeds / impacted tables (dashboards, queries, notebooks, people) | lineage |
# MAGIC | 5 | Column-level trace: which Cisco columns feed which downstream columns | `system.access.column_lineage` |
# MAGIC | 6 | Blind-spot checks for what lineage cannot see | view DDL, `system.query.history`, path lineage |
# MAGIC | 7 | Summary + optional persistence of all result sets as Delta tables | |
# MAGIC
# MAGIC **Known limits - read before trusting a clean result**
# MAGIC
# MAGIC * Lineage is only recorded for workloads that actually ran inside the lookback window (system tables
# MAGIC   keep 365 days). A quarterly or annual job that has not run in the window is invisible to steps 2-5.
# MAGIC * Lineage is not captured for some access patterns: direct cloud-path reads that bypass Unity Catalog,
# MAGIC   RDD / some UDF code paths, JDBC extracts by external tools, tables in `hive_metastore`.
# MAGIC   Step 6 narrows this gap but cannot close it.
# MAGIC * External consumers (Power BI, Tableau, reverse ETL, exports) show up only as a user / service
# MAGIC   principal and client application reading a table - not as the report they feed.
# MAGIC * You only see lineage for objects you have privileges on. Run as a metastore admin (or a principal
# MAGIC   with broad `BROWSE`) or the result will be silently incomplete.
# MAGIC
# MAGIC **To run:** fill in `1. Source schemas` and / or `1b. Source catalogs` (at least one), review the others, then Run All.
# MAGIC Works on serverless or a UC-enabled cluster.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0. Parameters

# COMMAND ----------

dbutils.widgets.text("source_schemas", "", "1. Source schemas (catalog.schema, comma-sep)")
dbutils.widgets.text("source_catalogs", "", "1b. Source catalogs - whole catalog, e.g. foreign catalogs on the Cisco DBs (comma-sep)")
dbutils.widgets.text("extra_source_tables", "", "2. Extra source tables (catalog.schema.table, comma-sep)")
dbutils.widgets.text("exclude_schemas", "", "3. Schemas to ignore downstream (catalog.schema, comma-sep)")
dbutils.widgets.text("lineage_lookback_days", "365", "4. Lineage lookback (days)")
dbutils.widgets.text("query_history_lookback_days", "90", "5. Query-history lookback (days)")
dbutils.widgets.text("keyword_regex", "cisco|wxcce|ucce|pcce|acqueon", "6. Keyword regex for blind-spot scan")
dbutils.widgets.text("output_schema", "", "7. Persist results to (catalog.schema, blank = don't)")
dbutils.widgets.text("output_prefix", "cisco_dep_", "8. Output table prefix")

# COMMAND ----------

import re
from collections import defaultdict, deque

from pyspark.sql import functions as F
from pyspark.sql import types as T

# System table locations. Change here if your account exposes them under other names.
TBL = {
    "table_lineage": "system.access.table_lineage",
    "column_lineage": "system.access.column_lineage",
    "query_history": "system.query.history",
    "tables": "system.information_schema.tables",
    "views": "system.information_schema.views",
    "jobs": "system.lakeflow.jobs",
    "pipelines": "system.lakeflow.pipelines",
}

MAX_TABLE_EDGES = 5_000_000   # safety valve for collecting lineage edges to the driver
MAX_COLUMN_EDGES = 5_000_000
MAX_ORIGINS_SHOWN = 25        # Cisco origin columns listed per downstream column


def _csv(name):
    return [x.strip().strip("`").lower() for x in dbutils.widgets.get(name).split(",") if x.strip()]


SOURCE_SCHEMAS = _csv("source_schemas")
SOURCE_CATALOGS = _csv("source_catalogs")
EXTRA_SOURCE_TABLES = _csv("extra_source_tables")
EXCLUDE_SCHEMAS = _csv("exclude_schemas")
LINEAGE_DAYS = int(dbutils.widgets.get("lineage_lookback_days"))
QH_DAYS = int(dbutils.widgets.get("query_history_lookback_days"))
KEYWORD_REGEX = dbutils.widgets.get("keyword_regex").strip()
OUTPUT_SCHEMA = dbutils.widgets.get("output_schema").strip()
OUTPUT_PREFIX = dbutils.widgets.get("output_prefix").strip()

if not SOURCE_SCHEMAS and not SOURCE_CATALOGS and not EXTRA_SOURCE_TABLES:
    raise ValueError("Set widget '1. Source schemas' (e.g. raw.cisco_wxcce, raw.acqueon) and / or '1b. Source catalogs'")
for c in SOURCE_CATALOGS:
    if not re.fullmatch(r"[\w-]+", c):
        raise ValueError(f"'{c}' must be a bare catalog name")
for s in SOURCE_SCHEMAS + EXCLUDE_SCHEMAS:
    if not re.fullmatch(r"[\w-]+\.[\w-]+", s):
        raise ValueError(f"'{s}' must be catalog.schema")
for t in EXTRA_SOURCE_TABLES:
    if t.count(".") != 2:
        raise ValueError(f"'{t}' must be catalog.schema.table")
if KEYWORD_REGEX:
    re.compile(KEYWORD_REGEX)

print("Source schemas      :", SOURCE_SCHEMAS)
print("Source catalogs     :", SOURCE_CATALOGS)
print("Extra source tables :", EXTRA_SOURCE_TABLES)
print("Ignored downstream  :", EXCLUDE_SCHEMAS)
print(f"Lineage lookback    : {LINEAGE_DAYS} days | query-history lookback: {QH_DAYS} days")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0b. Preflight - which system tables can this principal read?
# MAGIC `table_lineage` is mandatory. Everything else degrades gracefully: the matching section is skipped
# MAGIC and says so.

# COMMAND ----------

AVAILABLE = {}
for key, name in TBL.items():
    try:
        spark.table(name).limit(1).collect()
        AVAILABLE[key] = True
    except Exception as e:  # noqa: BLE001 - permission / not-enabled errors vary by runtime
        AVAILABLE[key] = False
        print(f"UNAVAILABLE  {name}: {str(e).splitlines()[0][:160]}")
print({k: ("ok" if v else "MISSING") for k, v in AVAILABLE.items()})
if not AVAILABLE["table_lineage"]:
    raise RuntimeError(f"{TBL['table_lineage']} is required. Ask an account admin to enable the `access` system schema.")


def _has_field(df, dotted):
    """True if a (possibly nested) column exists - system table schemas have evolved over time."""
    dtype = df.schema
    for part in dotted.split("."):
        if not isinstance(dtype, T.StructType) or part not in dtype.fieldNames():
            return False
        dtype = dtype[part].dataType
    return True


def _field(df, dotted, cast="string"):
    return f"CAST({dotted} AS {cast})" if _has_field(df, dotted) else f"CAST(NULL AS {cast})"


def with_entity_columns(df):
    """Normalise 'what ran this' into flat columns. Prefers entity_metadata, falls back to entity_type/entity_id."""
    job = _field(df, "entity_metadata.job_info.job_id")
    pipe = _field(df, "entity_metadata.dlt_pipeline_info.dlt_pipeline_id")
    nb = _field(df, "entity_metadata.notebook_id")
    dash = f"COALESCE({_field(df, 'entity_metadata.dashboard_id')}, {_field(df, 'entity_metadata.legacy_dashboard_id')})"
    qry = _field(df, "entity_metadata.sql_query_id")
    etype = _field(df, "entity_type")
    eid = _field(df, "entity_id")
    return (
        df.withColumn("job_id", F.expr(f"COALESCE({job}, CASE WHEN {etype} = 'JOB' THEN {eid} END)"))
        .withColumn("pipeline_id", F.expr(f"COALESCE({pipe}, CASE WHEN {etype} = 'PIPELINE' THEN {eid} END)"))
        .withColumn("notebook_id", F.expr(f"COALESCE({nb}, CASE WHEN {etype} = 'NOTEBOOK' THEN {eid} END)"))
        .withColumn("dashboard_id", F.expr(f"COALESCE({dash}, CASE WHEN {etype} LIKE '%DASHBOARD%' THEN {eid} END)"))
        .withColumn("sql_query_id", F.expr(f"COALESCE({qry}, CASE WHEN {etype} LIKE '%QUERY%' THEN {eid} END)"))
        .withColumn(
            "entity_kind",
            F.expr(
                """CASE WHEN pipeline_id IS NOT NULL THEN 'PIPELINE'
                        WHEN job_id IS NOT NULL THEN 'JOB'
                        WHEN dashboard_id IS NOT NULL THEN 'DASHBOARD'
                        WHEN sql_query_id IS NOT NULL THEN 'SQL_QUERY'
                        WHEN notebook_id IS NOT NULL THEN 'NOTEBOOK'
                        ELSE 'AD_HOC' END"""
            ),
        )
        .withColumn("entity_ref", F.expr("COALESCE(pipeline_id, job_id, dashboard_id, sql_query_id, notebook_id)"))
    )


# Lineage inside the lookback window, names lower-cased so joins are case-insensitive.
lineage = with_entity_columns(
    spark.table(TBL["table_lineage"])
    .where(F.col("event_date") >= F.date_sub(F.current_date(), LINEAGE_DAYS))
    .withColumn("src", F.lower("source_table_full_name"))
    .withColumn("tgt", F.lower("target_table_full_name"))
)
lineage.createOrReplaceTempView("_lin")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0c. External (foreign) catalogs - discovery and text-scan patterns
# MAGIC Lists every Lakehouse Federation catalog in the metastore with its connection, and whether it is in
# MAGIC widget 1b. A foreign catalog whose name or connection matches the keyword regex but is **not** selected
# MAGIC is called out - add it to widget 1b and re-run. Connection names of the selected catalogs are added to
# MAGIC the text scans in section 6, to catch code that goes through the connection directly
# MAGIC (`remote_query(...)`) rather than through the catalog. Best effort: needs the Databricks SDK.

# COMMAND ----------

CONNECTION_NAMES = set()
try:
    from databricks.sdk import WorkspaceClient

    _rows = []
    for _c in WorkspaceClient().catalogs.list():
        _conn = getattr(_c, "connection_name", None)
        if "FOREIGN" not in str(getattr(_c, "catalog_type", "")).upper() and not _conn:
            continue
        _name = (_c.name or "").lower()
        _selected = _name in SOURCE_CATALOGS
        _looks_cisco = bool(KEYWORD_REGEX and re.search(KEYWORD_REGEX, f"{_name} {_conn or ''} {_c.comment or ''}", re.I))
        if _selected and _conn:
            CONNECTION_NAMES.add(_conn.lower())
        _rows.append((_name, _conn, _c.owner, _c.comment, _selected, _looks_cisco and not _selected))
    if _rows:
        display(
            spark.createDataFrame(
                _rows, "catalog string, connection string, owner string, comment string, selected boolean, LOOKS_CISCO_BUT_NOT_SELECTED boolean"
            ).orderBy(F.desc("LOOKS_CISCO_BUT_NOT_SELECTED"), F.desc("selected"), "catalog")
        )
    else:
        print("No foreign catalogs visible to this principal.")
    _unknown = [c for c in SOURCE_CATALOGS if c not in {r[0] for r in _rows}]
    if _unknown:
        print("NOTE - in widget 1b but not a foreign catalog (fine if intentional):", _unknown)
except Exception as e:  # noqa: BLE001
    print("Foreign catalog discovery skipped:", str(e).splitlines()[0][:160])

# Regexes for scanning SQL text. Identifiers may be back-quoted; the look-behind stops `x.cisco_fed.` matching.
_ident = r"`?[\w$-]+`?"
_schema_patterns = []
for s in SOURCE_SCHEMAS:
    cat, sch = s.split(".")
    _schema_patterns.append(rf"(?<![\w.`])`?{re.escape(cat)}`?\s*\.\s*`?{re.escape(sch)}`?\s*\.")
_catalog_patterns = [rf"(?<![\w.`])`?{re.escape(c)}`?\s*\.\s*{_ident}\s*\." for c in SOURCE_CATALOGS]
_connection_patterns = [rf"\b{re.escape(c)}\b" for c in sorted(CONNECTION_NAMES)]
CATALOG_REGEX = "(?i)(" + "|".join(_catalog_patterns) + ")" if _catalog_patterns else None
TEXT_REGEX = "(?i)(" + "|".join(
    _schema_patterns + _catalog_patterns + _connection_patterns + ([KEYWORD_REGEX] if KEYWORD_REGEX else [])
) + ")"
print("Text-scan regex:", TEXT_REGEX)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Seed list - the Cisco / Acqueon source tables
# MAGIC Union of (a) what exists today in the source schemas and source catalogs, (b) names seen in lineage for those schemas
# MAGIC (catches tables that were dropped or renamed during the lookback window), (c) the extra tables widget.

# COMMAND ----------

_schema_list = ", ".join(f"'{s}'" for s in SOURCE_SCHEMAS)
_catalog_list = ", ".join(f"'{c}'" for c in SOURCE_CATALOGS)
schema_pred = " OR ".join(
    ([f"substring_index(t, '.', 2) IN ({_schema_list})"] if SOURCE_SCHEMAS else [])
    + ([f"substring_index(t, '.', 1) IN ({_catalog_list})"] if SOURCE_CATALOGS else [])
) or "FALSE"

seed_parts = [
    spark.sql("SELECT DISTINCT src AS t FROM _lin WHERE src IS NOT NULL").where(schema_pred),
    spark.sql("SELECT DISTINCT tgt AS t FROM _lin WHERE tgt IS NOT NULL").where(schema_pred),
]
if AVAILABLE["tables"]:
    seed_parts.append(
        spark.table(TBL["tables"])
        .select(F.lower(F.concat_ws(".", "table_catalog", "table_schema", "table_name")).alias("t"))
        .where(schema_pred)
    )
seed_df = seed_parts[0]
for p in seed_parts[1:]:
    seed_df = seed_df.unionByName(p)
SEEDS = {r.t for r in seed_df.distinct().collect()} | set(EXTRA_SOURCE_TABLES)

if not SEEDS:
    raise RuntimeError("No tables found in the source schemas / catalogs - check the widget values and your privileges.")


def schema_of(full_name):
    return full_name.rsplit(".", 1)[0]


seed_counts = defaultdict(int)
for t in SEEDS:
    seed_counts[schema_of(t)] += 1
print(f"{len(SEEDS)} source tables")
for s, n in sorted(seed_counts.items()):
    print(f"  {s:<60} {n}")
missing = [s for s in SOURCE_SCHEMAS if s not in seed_counts]
missing += [c for c in SOURCE_CATALOGS if not any(k.split(".")[0] == c for k in seed_counts)]
if missing:
    print("WARNING - no tables found for:", missing)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Walk table lineage downstream (all hops)
# MAGIC Breadth-first from the seeds over distinct `source -> target` edges. `hops = 1` reads Cisco data
# MAGIC directly; higher numbers inherit it through intermediate tables. `example_path` is one shortest route.

# COMMAND ----------

edges_df = spark.sql(
    """
    SELECT src, tgt, MAX(event_time) AS last_event_time, COUNT(*) AS events
    FROM _lin
    WHERE src IS NOT NULL AND tgt IS NOT NULL AND src <> tgt
    GROUP BY src, tgt
    """
).cache()
n_edges = edges_df.count()
print(f"{n_edges:,} distinct table-to-table edges in the last {LINEAGE_DAYS} days")
if n_edges > MAX_TABLE_EDGES:
    raise RuntimeError(f"{n_edges:,} edges exceeds MAX_TABLE_EDGES; shorten the lookback or raise the limit on a larger driver.")


def walk_downstream(edge_pairs, seeds, excluded_schemas=()):
    """BFS. Returns {node: (hops, parent)} for every non-seed node reachable from the seeds.

    Nodes in excluded schemas are neither reported nor traversed through.
    """
    children = defaultdict(list)
    for s, t in edge_pairs:
        children[s].append(t)
    excluded = set(excluded_schemas)
    seen = {}
    queue = deque((s, 0) for s in sorted(seeds))
    while queue:
        node, hops = queue.popleft()
        for child in children.get(node, ()):
            if child in seeds or child in seen or child.rsplit(".", 1)[0] in excluded:
                continue
            seen[child] = (hops + 1, node)
            queue.append((child, hops + 1))
    return seen


def path_to(node, reached):
    path = [node]
    while path[-1] in reached:
        path.append(reached[path[-1]][1])
    return " -> ".join(reversed(path))


edge_pairs = [(r.src, r.tgt) for r in edges_df.select("src", "tgt").collect()]
REACHED = walk_downstream(edge_pairs, SEEDS, EXCLUDE_SCHEMAS)
IMPACTED = set(REACHED)

direct_sources = defaultdict(set)
for s, t in edge_pairs:
    if s in SEEDS and t in IMPACTED:
        direct_sources[t].add(s)

impacted_base = spark.createDataFrame(
    [(t, h, path_to(t, REACHED), sorted(direct_sources.get(t, []))) for t, (h, _) in REACHED.items()],
    "table_full_name string, hops int, example_path string, direct_cisco_sources array<string>",
)
impacted_base.createOrReplaceTempView("_impacted")
spark.createDataFrame([(t,) for t in SEEDS], "table_full_name string").createOrReplaceTempView("_seeds")

by_hop = defaultdict(int)
for h, _ in REACHED.values():
    by_hop[h] += 1
print(f"{len(IMPACTED)} downstream tables/views impacted. By hop distance:", dict(sorted(by_hop.items())))

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2b. Impacted tables, enriched and ranked
# MAGIC Sorted so the things most likely to hurt on day 1 come first: read recently, by many people, close to
# MAGIC the source. `exists_now = false` means the table was dropped since the lineage was recorded.

# COMMAND ----------

if AVAILABLE["tables"]:
    spark.table(TBL["tables"]).select(
        F.lower(F.concat_ws(".", "table_catalog", "table_schema", "table_name")).alias("table_full_name"),
        "table_type", "table_owner", "data_source_format", "last_altered", "comment",
    ).createOrReplaceTempView("_meta")
else:
    spark.sql(
        """SELECT CAST(NULL AS string) table_full_name, CAST(NULL AS string) table_type, CAST(NULL AS string) table_owner,
                  CAST(NULL AS string) data_source_format, CAST(NULL AS timestamp) last_altered, CAST(NULL AS string) comment
           WHERE FALSE"""
    ).createOrReplaceTempView("_meta")

impacted_tables = spark.sql(
    """
    WITH writes AS (
      SELECT tgt AS table_full_name,
             MAX(event_time) AS last_written,
             COUNT(DISTINCT CASE WHEN entity_ref IS NOT NULL THEN concat(entity_kind, ':', entity_ref) END) AS writer_count
      FROM _lin WHERE tgt IS NOT NULL GROUP BY tgt
    ),
    reads AS (
      SELECT src AS table_full_name,
             MAX(event_time) AS last_read,
             COUNT(DISTINCT CASE WHEN event_date >= date_sub(current_date(), 30) THEN created_by END) AS readers_30d,
             COUNT(CASE WHEN event_date >= date_sub(current_date(), 30) THEN 1 END) AS read_events_30d,
             COUNT(DISTINCT CASE WHEN tgt IS NOT NULL AND tgt <> src THEN tgt END) AS downstream_tables
      FROM _lin WHERE src IS NOT NULL GROUP BY src
    )
    SELECT i.table_full_name, i.hops, m.table_type, m.table_owner, m.data_source_format,
           m.table_full_name IS NOT NULL AS exists_now,
           w.last_written, w.writer_count,
           r.last_read, COALESCE(r.readers_30d, 0) AS readers_30d, COALESCE(r.read_events_30d, 0) AS read_events_30d,
           COALESCE(r.downstream_tables, 0) AS downstream_tables,
           i.direct_cisco_sources, i.example_path, m.comment
    FROM _impacted i
    LEFT JOIN _meta m USING (table_full_name)
    LEFT JOIN writes w USING (table_full_name)
    LEFT JOIN reads r USING (table_full_name)
    ORDER BY readers_30d DESC, read_events_30d DESC, hops, table_full_name
    """
)
display(impacted_tables)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 2c. Tables and views pointing straight at the external (foreign) Cisco catalogs
# MAGIC Everything one hop from a catalog in widget 1b, found two independent ways: lineage (it was read in the
# MAGIC lookback window) and view SQL (the definition names the catalog, whether or not anyone queried it).
# MAGIC These break the moment the Cisco databases or the connection are switched off - no pipeline needs to
# MAGIC fail first, because a foreign catalog is a live pass-through to the source system.
# MAGIC `found_by = view_definition` only means a view that exists but has not been queried in the window.

# COMMAND ----------

foreign_dependents = None
if not SOURCE_CATALOGS:
    print("Widget 1b is blank - skipped.")
else:
    _fed = defaultdict(set)
    for _s, _t in edge_pairs:
        if _s.split(".")[0] in SOURCE_CATALOGS and _t not in SEEDS:
            _fed[_t].add(_s)
    by_lineage = spark.createDataFrame(
        [(t, sorted(v)) for t, v in _fed.items()], "table_full_name string, foreign_tables_read array<string>"
    )
    if AVAILABLE["views"]:
        by_ddl = (
            spark.table(TBL["views"])
            .where(~F.lower("table_catalog").isin(SOURCE_CATALOGS))
            .where(F.col("view_definition").rlike(CATALOG_REGEX))
            .select(
                F.lower(F.concat_ws(".", "table_catalog", "table_schema", "table_name")).alias("table_full_name"),
                F.array_sort(F.array_distinct(F.regexp_extract_all(F.lower("view_definition"), F.lit(CATALOG_REGEX), 1))).alias("references_in_sql"),
            )
        )
    else:
        print(f"{TBL['views']} not readable - lineage only.")
        by_ddl = spark.createDataFrame([], "table_full_name string, references_in_sql array<string>")
    foreign_dependents = (
        by_lineage.join(by_ddl, "table_full_name", "full_outer")
        .join(spark.table("_meta").select("table_full_name", "table_type", "table_owner"), "table_full_name", "left")
        .withColumn(
            "found_by",
            F.expr(
                """CASE WHEN foreign_tables_read IS NOT NULL AND references_in_sql IS NOT NULL THEN 'lineage + view_definition'
                        WHEN foreign_tables_read IS NOT NULL THEN 'lineage' ELSE 'view_definition' END"""
            ),
        )
        .select("table_full_name", "table_type", "table_owner", "found_by", "foreign_tables_read", "references_in_sql")
        .orderBy("found_by", "table_full_name")
    )
    display(foreign_dependents)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Writers - jobs, pipelines and notebooks that touch Cisco data
# MAGIC Every job / pipeline / notebook that reads a Cisco source or writes an impacted table. These are the
# MAGIC code assets that must be translated (or retired) for the cutover. `AD_HOC` rows with no entity are
# MAGIC interactive SQL - the person in `run_by` is the lead.

# COMMAND ----------

if AVAILABLE["jobs"]:
    spark.sql(
        f"""SELECT CAST(workspace_id AS string) AS workspace_id, CAST(job_id AS string) AS job_id,
                   max_by(name, change_time) AS entity_name
            FROM {TBL['jobs']} GROUP BY 1, 2"""
    ).createOrReplaceTempView("_jobs")
else:
    spark.sql("SELECT CAST(NULL AS string) workspace_id, CAST(NULL AS string) job_id, CAST(NULL AS string) entity_name WHERE FALSE").createOrReplaceTempView("_jobs")

if AVAILABLE["pipelines"]:
    spark.sql(
        f"""SELECT CAST(workspace_id AS string) AS workspace_id, CAST(pipeline_id AS string) AS pipeline_id,
                   max_by(name, change_time) AS entity_name
            FROM {TBL['pipelines']} GROUP BY 1, 2"""
    ).createOrReplaceTempView("_pipelines")
else:
    spark.sql("SELECT CAST(NULL AS string) workspace_id, CAST(NULL AS string) pipeline_id, CAST(NULL AS string) entity_name WHERE FALSE").createOrReplaceTempView("_pipelines")

# Every lineage event, flagged by how it relates to the seeds and the impacted set.
spark.sql(
    """
    SELECT l.*,
           ss.table_full_name IS NOT NULL AS reads_seed,
           si.table_full_name IS NOT NULL AS reads_impacted,
           ts.table_full_name IS NOT NULL AS writes_seed,
           ti.table_full_name IS NOT NULL AS writes_impacted
    FROM _lin l
    LEFT JOIN _seeds ss    ON l.src = ss.table_full_name
    LEFT JOIN _impacted si ON l.src = si.table_full_name
    LEFT JOIN _seeds ts    ON l.tgt = ts.table_full_name
    LEFT JOIN _impacted ti ON l.tgt = ti.table_full_name
    """
).createOrReplaceTempView("_linf")

# The subset that touches Cisco data.
spark.sql("SELECT * FROM _linf WHERE reads_seed OR reads_impacted OR writes_impacted").createOrReplaceTempView("_touch")

writers = spark.sql(
    """
    SELECT t.entity_kind, t.entity_ref AS entity_id,
           COALESCE(j.entity_name, p.entity_name) AS entity_name,
           t.workspace_id,
           MAX(t.event_time) AS last_run,
           COUNT(DISTINCT t.event_date) AS active_days,
           sort_array(collect_set(t.created_by)) AS run_by,
           sort_array(collect_set(CASE WHEN t.reads_seed THEN t.src END)) AS cisco_sources_read,
           sort_array(collect_set(CASE WHEN t.reads_impacted THEN t.src END)) AS impacted_tables_read,
           sort_array(collect_set(CASE WHEN t.writes_impacted THEN t.tgt END)) AS impacted_tables_written
    FROM _touch t
    LEFT JOIN _jobs j ON t.entity_kind = 'JOB' AND j.job_id = t.job_id AND j.workspace_id = CAST(t.workspace_id AS string)
    LEFT JOIN _pipelines p ON t.entity_kind = 'PIPELINE' AND p.pipeline_id = t.pipeline_id AND p.workspace_id = CAST(t.workspace_id AS string)
    WHERE t.tgt IS NOT NULL AND (t.reads_seed OR t.writes_impacted)
    GROUP BY ALL
    ORDER BY size(cisco_sources_read) DESC, last_run DESC
    """
)
display(writers)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Consumers - dashboards, saved queries, notebooks and people reading the data
# MAGIC Read-only access (no target table) to a Cisco source or an impacted table. `reads_cisco_directly`
# MAGIC is the urgent column: those consumers bypass the curated layer, so translating the curated
# MAGIC datasets will **not** fix them.

# COMMAND ----------

consumers = spark.sql(
    """
    SELECT t.entity_kind, t.entity_ref AS entity_id,
           COALESCE(j.entity_name, p.entity_name) AS entity_name,
           t.workspace_id,
           bool_or(t.reads_seed) AS reads_cisco_directly,
           MAX(t.event_time) AS last_read,
           COUNT(*) AS read_events,
           COUNT(CASE WHEN t.event_date >= date_sub(current_date(), 30) THEN 1 END) AS read_events_30d,
           sort_array(collect_set(t.created_by)) AS read_by,
           sort_array(collect_set(t.src)) AS tables_read
    FROM _touch t
    LEFT JOIN _jobs j ON t.entity_kind = 'JOB' AND j.job_id = t.job_id AND j.workspace_id = CAST(t.workspace_id AS string)
    LEFT JOIN _pipelines p ON t.entity_kind = 'PIPELINE' AND p.pipeline_id = t.pipeline_id AND p.workspace_id = CAST(t.workspace_id AS string)
    WHERE t.tgt IS NULL AND (t.reads_seed OR t.reads_impacted)
    GROUP BY ALL
    ORDER BY reads_cisco_directly DESC, read_events_30d DESC, last_read DESC
    """
)
display(consumers)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 4b. Resolve dashboard names (best effort, this workspace only)
# MAGIC System tables carry dashboard IDs but not titles. This looks them up through the SDK; IDs from
# MAGIC other workspaces or ones you cannot see stay unresolved.

# COMMAND ----------

dashboard_names = {}
try:
    from databricks.sdk import WorkspaceClient

    _w = WorkspaceClient()
    _ids = [r.entity_id for r in consumers.where("entity_kind = 'DASHBOARD'").select("entity_id").distinct().limit(500).collect()]
    for _id in _ids:
        try:
            dashboard_names[_id] = _w.lakeview.get(_id).display_name
        except Exception:  # noqa: BLE001 - legacy dashboard, other workspace, or no access
            pass
    print(f"Resolved {len(dashboard_names)} of {len(_ids)} dashboard ids")
except Exception as e:  # noqa: BLE001
    print("Dashboard name lookup skipped:", str(e).splitlines()[0][:160])

if dashboard_names:
    names_df = spark.createDataFrame(list(dashboard_names.items()), "entity_id string, dashboard_name string")
    consumers = (
        consumers.join(names_df, "entity_id", "left")
        .withColumn("entity_name", F.coalesce("entity_name", "dashboard_name"))
        .drop("dashboard_name")
        .orderBy(F.desc("reads_cisco_directly"), F.desc("read_events_30d"))
    )
    display(consumers.where("entity_kind = 'DASHBOARD'"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Column-level impact
# MAGIC Traces each Cisco source column through column lineage to every downstream column it feeds.
# MAGIC Use `column_impact` to check the NICE field mapping field by field, and `column_edges` to follow a
# MAGIC specific transformation chain hop by hop.

# COMMAND ----------

def walk_columns(col_edges, seed_tables):
    """Propagate the set of originating seed columns along column edges (worklist fixed point).

    col_edges: iterable of ((table, column), (table, column)). Returns {node: (hops, origins)} for
    non-seed nodes, where origins is the set of seed (table, column) pairs that feed the node.
    """
    children = defaultdict(set)
    for s, t in col_edges:
        children[s].add(t)
    origins = defaultdict(set)
    hops = {}
    queue = deque()
    for node in list(children):
        if node[0] in seed_tables:
            origins[node] = {node}
            hops[node] = 0
            queue.append(node)
    while queue:
        node = queue.popleft()
        for child in children.get(node, ()):
            if child[0] in seed_tables:
                continue
            before = len(origins[child])
            origins[child] |= origins[node]
            new_hops = hops[node] + 1
            if child not in hops or new_hops < hops[child]:
                hops[child] = new_hops
                queue.append(child)
            elif len(origins[child]) > before:
                queue.append(child)
    return {n: (hops[n], origins[n]) for n in hops if n[0] not in seed_tables}


column_impact = column_edges = None
if not AVAILABLE["column_lineage"]:
    print(f"SKIPPED - {TBL['column_lineage']} is not readable.")
else:
    column_edges = spark.sql(
        f"""
        WITH ce AS (
          SELECT lower(source_table_full_name) AS source_table, lower(source_column_name) AS source_column,
                 lower(target_table_full_name) AS target_table, lower(target_column_name) AS target_column,
                 event_time
          FROM {TBL['column_lineage']}
          WHERE event_date >= date_sub(current_date(), {LINEAGE_DAYS})
            AND source_column_name IS NOT NULL AND target_column_name IS NOT NULL
        ),
        scope AS (SELECT table_full_name FROM _seeds UNION SELECT table_full_name FROM _impacted)
        SELECT ce.source_table, ce.source_column, ce.target_table, ce.target_column,
               MAX(ce.event_time) AS last_event_time
        FROM ce
        JOIN _impacted ti ON ce.target_table = ti.table_full_name
        JOIN scope sc     ON ce.source_table = sc.table_full_name
        GROUP BY ALL
        """
    ).cache()
    n_col_edges = column_edges.count()
    print(f"{n_col_edges:,} column edges inside the impacted subgraph")
    if n_col_edges > MAX_COLUMN_EDGES:
        raise RuntimeError("Column edge count exceeds MAX_COLUMN_EDGES; shorten the lookback or raise the limit.")

    col_pairs = [
        ((r.source_table, r.source_column), (r.target_table, r.target_column))
        for r in column_edges.select("source_table", "source_column", "target_table", "target_column").collect()
    ]
    col_reached = walk_columns(col_pairs, SEEDS)
    column_impact = spark.createDataFrame(
        [
            (t, c, h, len(o), [f"{ot}.{oc}" for ot, oc in sorted(o)[:MAX_ORIGINS_SHOWN]])
            for (t, c), (h, o) in col_reached.items()
        ],
        "table_full_name string, column_name string, hops int, cisco_origin_count int, cisco_origin_columns array<string>",
    ).orderBy("table_full_name", "column_name")
    column_impact.createOrReplaceTempView("_column_impact")
    print(f"{len(col_reached):,} downstream columns across {len({t for t, _ in col_reached}):,} tables carry Cisco data")
    display(column_impact)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 5b. Cisco source columns ranked by blast radius
# MAGIC The source fields with the most downstream dependants - prioritise these when validating the NICE mapping.
# MAGIC
# MAGIC Impacted tables with **no** column lineage are listed after it: Cisco data reaches them but the
# MAGIC column mapping could not be traced (typical for `SELECT *` into files, UDF-heavy code, or path
# MAGIC writes). They need a manual look.

# COMMAND ----------

if column_impact is not None:
    display(
        spark.sql(
            """
            SELECT origin AS cisco_source_column,
                   COUNT(*) AS downstream_columns,
                   COUNT(DISTINCT table_full_name) AS downstream_tables,
                   MAX(hops) AS max_hops
            FROM _column_impact LATERAL VIEW explode(cisco_origin_columns) o AS origin
            GROUP BY origin ORDER BY downstream_columns DESC
            """
        )
    )
    no_column_lineage = spark.sql(
        """SELECT i.table_full_name, i.hops, i.example_path FROM _impacted i
           LEFT ANTI JOIN (SELECT DISTINCT table_full_name FROM _column_impact) c USING (table_full_name)
           ORDER BY i.hops, i.table_full_name"""
    )
    display(no_column_lineage)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Blind-spot checks
# MAGIC Lineage is good but not complete. These four checks look for Cisco dependencies it may have missed.
# MAGIC Anything listed here is **not** in the sections above and deserves a manual review.

# COMMAND ----------

# MAGIC %md
# MAGIC ### 6a. Views whose SQL references a Cisco schema / catalog / connection but that lineage did not flag
# MAGIC Typically views nobody queried inside the lookback window - still broken on day 1 if the source goes away.

# COMMAND ----------

unflagged_views = None
if not AVAILABLE["views"]:
    print(f"SKIPPED - {TBL['views']} is not readable.")
else:
    unflagged_views = (
        spark.table(TBL["views"])
        .select(
            F.lower(F.concat_ws(".", "table_catalog", "table_schema", "table_name")).alias("table_full_name"),
            "view_definition",
        )
        .where(F.col("view_definition").rlike(TEXT_REGEX))
        .withColumn("matched_text", F.regexp_extract("view_definition", TEXT_REGEX, 1))
        .join(impacted_base.select("table_full_name"), "table_full_name", "left_anti")
        .join(spark.table("_seeds"), "table_full_name", "left_anti")
        .select("table_full_name", "matched_text", F.substring("view_definition", 1, 2000).alias("view_definition_head"))
        .orderBy("table_full_name")
    )
    display(unflagged_views)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 6b. Query history: statements mentioning Cisco schemas / catalogs / connections / keywords with no lineage record
# MAGIC Catches SQL-warehouse and serverless traffic that lineage did not attribute - BI tools, JDBC/ODBC
# MAGIC extracts, failed-but-scheduled queries. Grouped by who / what client / which asset. Keyword hits can be
# MAGIC false positives (the word in a comment or column alias); read `sample_statement` before acting.

# COMMAND ----------

unattributed_queries = None
if not AVAILABLE["query_history"]:
    print(f"SKIPPED - {TBL['query_history']} is not readable.")
else:
    qh = spark.table(TBL["query_history"])
    qh_time = "start_time" if _has_field(qh, "start_time") else "end_time"
    qh_src = lambda f: _field(qh, f"query_source.{f}")  # noqa: E731
    unattributed_queries = (
        qh.where(F.col(qh_time) >= F.date_sub(F.current_date(), QH_DAYS))
        .where(F.col("statement_text").rlike(TEXT_REGEX))
        # the scan's own statements contain the regex - drop them
        .where(~F.col("statement_text").contains(TEXT_REGEX))
        .join(lineage.where("statement_id IS NOT NULL").select("statement_id").distinct(), "statement_id", "left_anti")
        .withColumn("matched_text", F.lower(F.regexp_extract("statement_text", TEXT_REGEX, 1)))
        .withColumn("job_id", F.expr(qh_src("job_info.job_id")))
        .withColumn("dashboard_id", F.expr(f"COALESCE({qh_src('dashboard_id')}, {qh_src('legacy_dashboard_id')})"))
        .withColumn("sql_query_id", F.expr(qh_src("sql_query_id")))
        .withColumn("notebook_id", F.expr(qh_src("notebook_id")))
        .withColumn("alert_id", F.expr(qh_src("alert_id")))
        .withColumn("genie_space_id", F.expr(qh_src("genie_space_id")))
        .groupBy(
            "executed_by", F.expr(_field(qh, "client_application")).alias("client_application"),
            "job_id", "dashboard_id", "sql_query_id", "notebook_id", "alert_id", "genie_space_id",
        )
        .agg(
            F.count("*").alias("statements"),
            F.max(qh_time).alias("last_seen"),
            F.sort_array(F.collect_set("matched_text")).alias("matched"),
            F.sort_array(F.collect_set(F.expr(_field(qh, "statement_type")))).alias("statement_types"),
            F.sort_array(F.collect_set(F.expr(_field(qh, "execution_status")))).alias("statuses"),
            F.substring(F.max_by("statement_text", qh_time), 1, 1500).alias("sample_statement"),
        )
        .orderBy(F.desc("statements"))
    )
    display(unattributed_queries)

# COMMAND ----------

# MAGIC %md
# MAGIC ### 6c. Cisco tables hiding outside the source schemas
# MAGIC Tables / views elsewhere in the metastore whose **name** matches the keyword regex but which are neither
# MAGIC a seed nor already flagged. Often manual copies, backups or sandbox extracts that people report from.

# COMMAND ----------

name_matches = None
if AVAILABLE["tables"] and KEYWORD_REGEX:
    name_matches = (
        spark.table("_meta")
        .where(F.col("table_full_name").rlike(f"(?i)({KEYWORD_REGEX})"))
        .join(impacted_base.select("table_full_name"), "table_full_name", "left_anti")
        .join(spark.table("_seeds"), "table_full_name", "left_anti")
        .select("table_full_name", "table_type", "table_owner", "last_altered")
        .orderBy("table_full_name")
    )
    display(name_matches)
else:
    print("SKIPPED - information_schema.tables not readable or keyword regex is blank.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### 6d. Path-based access to the storage behind the Cisco tables
# MAGIC First result: the cloud paths lineage has seen feeding or backing the Cisco source tables.
# MAGIC Second result: anything **outside** the seeds and the impacted set that reads from or writes to those same
# MAGIC paths - i.e. code that goes around the tables straight to the files.

# COMMAND ----------

path_consumers = None
if _has_field(lineage, "source_path") and _has_field(lineage, "target_path"):
    seed_paths = spark.sql(
        """
        SELECT DISTINCT path FROM (
          SELECT source_path AS path FROM _linf WHERE writes_seed
          UNION ALL SELECT target_path AS path FROM _linf WHERE writes_seed
          UNION ALL SELECT source_path AS path FROM _linf WHERE reads_seed
        ) WHERE path IS NOT NULL
        """
    )
    seed_paths.createOrReplaceTempView("_seed_paths")
    display(seed_paths.orderBy("path"))

    path_consumers = spark.sql(
        """
        SELECT l.entity_kind, l.entity_ref AS entity_id, l.workspace_id,
               COALESCE(l.tgt, l.target_path) AS writes_to,
               COALESCE(l.src, l.source_path) AS reads_from,
               MAX(l.event_time) AS last_event_time,
               sort_array(collect_set(l.created_by)) AS run_by
        FROM _linf l
        LEFT JOIN _seed_paths sp ON l.source_path = sp.path
        LEFT JOIN _seed_paths tp ON l.target_path = tp.path
        WHERE (sp.path IS NOT NULL OR tp.path IS NOT NULL)
          AND NOT (l.reads_seed OR l.writes_seed OR l.writes_impacted)
        GROUP BY ALL
        ORDER BY last_event_time DESC
        """
    )
    display(path_consumers)
else:
    print("SKIPPED - this lineage table has no source_path / target_path columns.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Summary

# COMMAND ----------

def _n(df, where=None):
    if df is None:
        return "skipped"
    return (df.where(where) if where else df).count()


summary = [
    ("Cisco / Acqueon source tables (seeds)", len(SEEDS)),
    ("Downstream tables / views impacted", len(IMPACTED)),
    ("  ...reading Cisco directly (1 hop)", by_hop.get(1, 0)),
    ("  ...pointing at the external Cisco catalogs", _n(foreign_dependents)),
    ("  ...read by someone in the last 30 days", _n(impacted_tables, "readers_30d > 0")),
    ("Jobs writing Cisco-derived data", _n(writers, "entity_kind = 'JOB'")),
    ("Pipelines writing Cisco-derived data", _n(writers, "entity_kind = 'PIPELINE'")),
    ("Notebooks / ad-hoc writers", _n(writers, "entity_kind NOT IN ('JOB', 'PIPELINE')")),
    ("Dashboards reading Cisco or impacted tables", _n(consumers, "entity_kind = 'DASHBOARD'")),
    ("Saved SQL queries reading them", _n(consumers, "entity_kind = 'SQL_QUERY'")),
    ("Consumers reading Cisco schemas DIRECTLY", _n(consumers, "reads_cisco_directly")),
    ("Downstream columns carrying Cisco data", _n(column_impact)),
    ("Blind spot: views not flagged by lineage", _n(unflagged_views)),
    ("Blind spot: query-history groups without lineage", _n(unattributed_queries)),
    ("Blind spot: Cisco-named tables outside source schemas", _n(name_matches)),
    ("Blind spot: path-based readers / writers", _n(path_consumers)),
]
display(spark.createDataFrame([(k, str(v)) for k, v in summary], "metric string, value string"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Impacted tables by schema
# MAGIC A quick map of where the exposure sits - hand each row to the owning team.

# COMMAND ----------

display(
    impacted_tables.withColumn("schema", F.expr("substring_index(table_full_name, '.', 2)"))
    .groupBy("schema")
    .agg(
        F.count("*").alias("impacted_tables"),
        F.min("hops").alias("min_hops"),
        F.sum(F.expr("CASE WHEN readers_30d > 0 THEN 1 ELSE 0 END")).alias("actively_read_30d"),
        F.max("last_written").alias("last_written"),
        F.sort_array(F.collect_set("table_owner")).alias("owners"),
    )
    .orderBy(F.desc("impacted_tables"))
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Persist results (optional)
# MAGIC Set widget 7 to a `catalog.schema` you can write to. Each run overwrites the tables and stamps
# MAGIC `scan_ts`, so you can re-run daily up to cutover and diff the runs.

# COMMAND ----------

outputs = {
    "impacted_tables": impacted_tables,
    "foreign_catalog_dependents": foreign_dependents,
    "writers": writers,
    "consumers": consumers,
    "column_impact": column_impact,
    "column_edges": column_edges,
    "blindspot_views": unflagged_views,
    "blindspot_queries": unattributed_queries,
    "blindspot_named_tables": name_matches,
    "blindspot_path_access": path_consumers,
}
if not OUTPUT_SCHEMA:
    print("Widget 7 is blank - nothing persisted.")
else:
    if OUTPUT_SCHEMA.count(".") != 1:
        raise ValueError("output_schema must be catalog.schema")
    for name, df in outputs.items():
        if df is None:
            continue
        target = f"{OUTPUT_SCHEMA}.{OUTPUT_PREFIX}{name}"
        (
            df.withColumn("scan_ts", F.current_timestamp())
            .write.mode("overwrite")
            .option("overwriteSchema", "true")
            .saveAsTable(target)
        )
        print("wrote", target)
