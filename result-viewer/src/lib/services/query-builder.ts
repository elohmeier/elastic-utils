import type { Filter, TimeRange } from "$lib/stores/app-state.svelte";

export interface QueryParams {
  searchQuery: string;
  filters: Filter[];
  timeRange: TimeRange;
  visibleColumns: string[];
  sortColumn: string | null;
  sortDirection: "asc" | "desc";
  offset: number;
  limit: number;
}

function escapeSql(str: string): string {
  return str.replace(/'/g, "''").replace(/\\/g, "\\\\");
}

/**
 * Quote a field name for SQL. Handles nested paths like _source."@timestamp"
 */
function quoteField(field: string): string {
  // If already contains quotes or dots (nested path), use as-is
  if (field.includes("\"") || field.includes(".")) {
    return field;
  }
  return `"${field}"`;
}

function formatValue(value: string | number | boolean): string {
  if (typeof value === "string") {
    return `'${escapeSql(value)}'`;
  }
  if (typeof value === "boolean") {
    return value ? "TRUE" : "FALSE";
  }
  return String(value);
}

function buildFilterClause(filter: Filter): string {
  const field = quoteField(filter.field);
  switch (filter.operator) {
    case "eq":
      return `${field} = ${formatValue(filter.value)}`;
    case "neq":
      return `${field} != ${formatValue(filter.value)}`;
    case "contains":
      return `CAST(${field} AS VARCHAR) ILIKE '%${escapeSql(String(filter.value))}%'`;
    case "gt":
      return `${field} > ${formatValue(filter.value)}`;
    case "lt":
      return `${field} < ${formatValue(filter.value)}`;
    case "gte":
      return `${field} >= ${formatValue(filter.value)}`;
    case "lte":
      return `${field} <= ${formatValue(filter.value)}`;
    case "exists":
      return `${field} IS NOT NULL`;
    case "not_exists":
      return `${field} IS NULL`;
    default:
      return "1=1";
  }
}

export function buildWhereClause(params: {
  searchQuery: string;
  filters: Filter[];
  timeRange: TimeRange;
  allColumns?: string[];
}): string {
  const whereClauses: string[] = [];

  // Time range filter
  if (params.timeRange.from && params.timeRange.field) {
    const field = quoteField(params.timeRange.field);
    whereClauses.push(`${field} >= '${params.timeRange.from.toISOString()}'`);
  }
  if (params.timeRange.to && params.timeRange.field) {
    const field = quoteField(params.timeRange.field);
    whereClauses.push(`${field} <= '${params.timeRange.to.toISOString()}'`);
  }

  // Field filters
  for (const filter of params.filters) {
    whereClauses.push(buildFilterClause(filter));
  }

  // Full-text search
  if (params.searchQuery && params.allColumns && params.allColumns.length > 0) {
    const searchClauses = params.allColumns
      .map((col) => `COALESCE(CAST("${col}" AS VARCHAR), '') ILIKE '%${escapeSql(params.searchQuery)}%'`)
      .join(" OR ");
    whereClauses.push(`(${searchClauses})`);
  }

  return whereClauses.length > 0 ? `WHERE ${whereClauses.join(" AND ")}` : "";
}

/**
 * Convert a field path to a column alias for SELECT
 * e.g., "_source"."message" -> _source_message
 */
function pathToAlias(path: string): string {
  return path.replace(/"/g, "").replace(/\./g, "_");
}

export function buildDataQuery(params: QueryParams, allColumns: string[]): string {
  let columns: string;

  if (params.visibleColumns.length > 0) {
    // Build column selections with aliases for nested paths
    const colExprs = params.visibleColumns.map((c) => {
      const quoted = quoteField(c);
      // If it's a nested path (contains .), add an alias
      if (c.includes(".") || c.includes("\"")) {
        const alias = pathToAlias(c);
        return `${quoted} AS "${alias}"`;
      }
      return quoted;
    });
    columns = colExprs.join(", ");
  } else {
    columns = "*";
  }

  const whereClause = buildWhereClause({
    ...params,
    allColumns,
  });

  let sql = `SELECT ${columns} FROM data ${whereClause}`;

  if (params.sortColumn) {
    sql += ` ORDER BY ${quoteField(params.sortColumn)} ${params.sortDirection.toUpperCase()}`;
  }

  sql += ` LIMIT ${params.limit} OFFSET ${params.offset}`;

  return sql;
}

/**
 * Get the result column name for a given path
 */
export function getResultColumnName(path: string): string {
  if (path.includes(".") || path.includes("\"")) {
    return pathToAlias(path);
  }
  return path;
}

export function buildCountQuery(params: {
  searchQuery: string;
  filters: Filter[];
  timeRange: TimeRange;
  allColumns: string[];
}): string {
  const whereClause = buildWhereClause(params);
  return `SELECT COUNT(*) as cnt FROM data ${whereClause}`;
}
