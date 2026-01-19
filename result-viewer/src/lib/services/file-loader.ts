import { getConnection, getDuckDB, resetConnection } from "$lib/duckdb";
import * as duckdb from "@duckdb/duckdb-wasm";

export interface FieldSchema {
  name: string;
  type: string;
  nullable: boolean;
}

export interface LoadedFile {
  name: string;
  size: number;
  rowCount: number;
  schema: FieldSchema[];
}

/**
 * Check if value is a TypedArray
 */
function isTypedArray(value: unknown): value is ArrayBufferView {
  return (
    value instanceof Int8Array
    || value instanceof Uint8Array
    || value instanceof Uint8ClampedArray
    || value instanceof Int16Array
    || value instanceof Uint16Array
    || value instanceof Int32Array
    || value instanceof Uint32Array
    || value instanceof Float32Array
    || value instanceof Float64Array
    || value instanceof BigInt64Array
    || value instanceof BigUint64Array
  );
}

/**
 * Recursively convert Arrow Vectors and other Arrow types to plain JS values
 */
function toPlainJS(value: unknown): unknown {
  if (value === null || value === undefined) {
    return value;
  }

  // Handle BigInt
  if (typeof value === "bigint") {
    // Convert to number if safe, otherwise to string
    if (value >= Number.MIN_SAFE_INTEGER && value <= Number.MAX_SAFE_INTEGER) {
      return Number(value);
    }
    return value.toString();
  }

  // Handle TypedArrays (including BigInt64Array)
  if (isTypedArray(value)) {
    return Array.from(value as unknown as ArrayLike<unknown>, toPlainJS);
  }

  // Check if it's an Arrow Vector (has toArray method and is iterable with get/length)
  if (
    typeof value === "object"
    && value !== null
    && "get" in value
    && "length" in value
    && typeof (value as { get: unknown }).get === "function"
    && typeof (value as { length: unknown }).length === "number"
  ) {
    const result: unknown[] = [];
    const len = (value as { length: number }).length;
    const getter = (value as { get: (i: number) => unknown }).get.bind(value);
    for (let i = 0; i < len; i++) {
      result.push(toPlainJS(getter(i)));
    }
    return result;
  }

  // Handle regular arrays
  if (Array.isArray(value)) {
    return value.map(toPlainJS);
  }

  // Handle plain objects (including nested structs)
  if (typeof value === "object" && value !== null) {
    // Skip if it looks like an internal Arrow structure
    if ("typeId" in value || ("data" in value && "_offsets" in value)) {
      return null;
    }
    const result: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      result[k] = toPlainJS(v);
    }
    return result;
  }

  return value;
}

/**
 * Convert Arrow table rows to plain JS objects
 */
function convertRows(rows: Record<string, unknown>[]): Record<string, unknown>[] {
  return rows.map((row) => {
    const result: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(row)) {
      result[key] = toPlainJS(value);
    }
    return result;
  });
}

export async function loadJSONLFile(file: File): Promise<LoadedFile> {
  const db = await getDuckDB();
  const conn = await getConnection();

  // Drop existing table if any
  await conn.query("DROP TABLE IF EXISTS data");

  // Register the file with DuckDB using browser File API
  await db.registerFileHandle(file.name, file, duckdb.DuckDBDataProtocol.BROWSER_FILEREADER, true);

  // Create a table from the JSONL file
  await conn.query(`
		CREATE TABLE data AS
		SELECT * FROM read_ndjson('${file.name}',
			auto_detect = true,
			ignore_errors = true,
			maximum_object_size = 33554432
		)
	`);

  // Get row count
  const countResult = await conn.query("SELECT COUNT(*) as cnt FROM data");
  const countArray = countResult.toArray();
  const rowCount = Number(countArray[0]?.cnt ?? 0);

  // Get schema
  const schemaResult = await conn.query("DESCRIBE data");
  const schemaArray = schemaResult.toArray();
  const schema: FieldSchema[] = schemaArray.map((row: Record<string, unknown>) => ({
    name: String(row.column_name),
    type: String(row.column_type),
    nullable: row.null === "YES",
  }));

  return {
    name: file.name,
    size: file.size,
    rowCount,
    schema,
  };
}

export async function queryData(
  sql: string,
): Promise<{ rows: Record<string, unknown>[]; queryTime: number }> {
  const conn = await getConnection();
  const start = performance.now();
  const result = await conn.query(sql);
  const queryTime = performance.now() - start;
  const rawRows = result.toArray() as Record<string, unknown>[];
  const rows = convertRows(rawRows);
  return { rows, queryTime };
}

export async function getRowById(rowIndex: number): Promise<Record<string, unknown> | null> {
  const conn = await getConnection();
  const result = await conn.query(`SELECT * FROM data LIMIT 1 OFFSET ${rowIndex}`);
  const rawRows = result.toArray() as Record<string, unknown>[];
  const rows = convertRows(rawRows);
  return rows[0] ?? null;
}
