import { createRequire } from "node:module";
import path from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);

// Use the blocking Node.js build of DuckDB WASM for testing
const duckdb = require("@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs");

const DUCKDB_DIST = path.dirname(
  require.resolve("@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm"),
);

let db: any;
let conn: any;

beforeAll(async () => {
  const bundles = {
    mvp: {
      mainModule: path.join(DUCKDB_DIST, "duckdb-mvp.wasm"),
      mainWorker: path.join(DUCKDB_DIST, "duckdb-node-mvp.worker.cjs"),
    },
    eh: {
      mainModule: path.join(DUCKDB_DIST, "duckdb-eh.wasm"),
      mainWorker: path.join(DUCKDB_DIST, "duckdb-node-eh.worker.cjs"),
    },
  };
  const logger = new duckdb.ConsoleLogger();
  db = await duckdb.createDuckDB(bundles, logger, duckdb.NODE_RUNTIME);
  await db.instantiate();
  await db.open({});
  conn = await db.connect();
}, 30_000);

afterAll(async () => {
  if (conn) conn.close();
  if (db) await db.dropFiles();
});

// Zstd-compressed version of: {"name":"alice","age":30}\n{"name":"bob","age":25}\n
// Generated with: echo '...' | zstd -c | base64
const ZSTD_FIXTURE_B64 = "KLUv/QRYXQEAFAJ7Im5hbWUiOiJhbGljZSIsImFnZSI6MzB9CmJvYjI1fQoCADvJsIs5BtDkftc=";

describe("DuckDB WASM NDJSON loading", () => {
  it("reads plain NDJSON from a registered buffer", async () => {
    const jsonl = "{\"name\":\"alice\",\"age\":30}\n{\"name\":\"bob\",\"age\":25}\n";
    const buffer = new TextEncoder().encode(jsonl);

    await db.registerFileBuffer("plain.jsonl", buffer);
    await conn.query("DROP TABLE IF EXISTS test_plain");
    await conn.query(`
      CREATE TABLE test_plain AS
      SELECT * FROM read_ndjson('plain.jsonl',
        auto_detect = true,
        ignore_errors = true,
        maximum_object_size = 33554432
      )
    `);

    const result = await conn.query(
      "SELECT * FROM test_plain ORDER BY name",
    );
    const rows = result.toArray();
    expect(rows).toHaveLength(2);
    expect(rows[0].name).toBe("alice");
    expect(rows[0].age).toBe(30n);
    expect(rows[1].name).toBe("bob");
    expect(rows[1].age).toBe(25n);
  });

  it("reads zstd-compressed NDJSON after loading parquet extension", async () => {
    await conn.query("INSTALL parquet; LOAD parquet;");

    const compressed = Uint8Array.from(atob(ZSTD_FIXTURE_B64), (c) => c.charCodeAt(0));
    await db.registerFileBuffer("native.jsonl.zst", compressed);
    await conn.query("DROP TABLE IF EXISTS test_zstd_native");
    await conn.query(`
      CREATE TABLE test_zstd_native AS
      SELECT * FROM read_ndjson('native.jsonl.zst',
        auto_detect = true,
        ignore_errors = true,
        maximum_object_size = 33554432,
        compression = 'zstd'
      )
    `);

    const result = await conn.query(
      "SELECT * FROM test_zstd_native ORDER BY name",
    );
    const rows = result.toArray();
    expect(rows).toHaveLength(2);
    expect(rows[0].name).toBe("alice");
    expect(rows[0].age).toBe(30n);
    expect(rows[1].name).toBe("bob");
    expect(rows[1].age).toBe(25n);
  });

  it("reads zstd-compressed NDJSON via JS decompression fallback", async () => {
    const { decompress } = await import("fzstd");
    const compressed = Uint8Array.from(atob(ZSTD_FIXTURE_B64), (c) => c.charCodeAt(0));
    const decompressed = decompress(compressed);

    await db.registerFileBuffer("fallback.jsonl", decompressed);
    await conn.query("DROP TABLE IF EXISTS test_zstd_fallback");
    await conn.query(`
      CREATE TABLE test_zstd_fallback AS
      SELECT * FROM read_ndjson('fallback.jsonl',
        auto_detect = true,
        ignore_errors = true,
        maximum_object_size = 33554432
      )
    `);

    const result = await conn.query(
      "SELECT * FROM test_zstd_fallback ORDER BY name",
    );
    const rows = result.toArray();
    expect(rows).toHaveLength(2);
    expect(rows[0].name).toBe("alice");
    expect(rows[0].age).toBe(30n);
    expect(rows[1].name).toBe("bob");
    expect(rows[1].age).toBe(25n);
  });
});
