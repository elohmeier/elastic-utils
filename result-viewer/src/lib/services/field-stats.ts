import { getConnection } from "$lib/duckdb";

export interface FieldStatistics {
  field: string;
  uniqueCount: number;
  nullCount: number;
  topValues: Array<{ value: string; count: number }>;
}

/**
 * Get statistics for a field. Supports nested paths like "_source"."message"
 */
export async function getFieldStatistics(fieldPath: string): Promise<FieldStatistics> {
  const conn = await getConnection();

  // fieldPath is already properly quoted (e.g., "_source"."message")
  // Use it directly in queries

  // Get unique count and null count
  const statsResult = await conn.query(`
		SELECT
			COUNT(DISTINCT ${fieldPath}) as unique_count,
			COUNT(*) - COUNT(${fieldPath}) as null_count
		FROM data
	`);
  const statsArray = statsResult.toArray();
  const uniqueCount = Number(statsArray[0]?.unique_count ?? 0);
  const nullCount = Number(statsArray[0]?.null_count ?? 0);

  // Get top values
  const topValuesResult = await conn.query(`
		SELECT
			CAST(${fieldPath} AS VARCHAR) as value,
			COUNT(*) as count
		FROM data
		WHERE ${fieldPath} IS NOT NULL
		GROUP BY ${fieldPath}
		ORDER BY count DESC
		LIMIT 10
	`);

  const topValuesArray = topValuesResult.toArray() as Array<{ value: string; count: bigint }>;
  const topValues = topValuesArray.map((row) => ({
    value: String(row.value ?? ""),
    count: Number(row.count),
  }));

  return {
    field: fieldPath,
    uniqueCount,
    nullCount,
    topValues,
  };
}

export async function getNumericFieldStats(fieldName: string): Promise<
  {
    min: number;
    max: number;
    avg: number;
  } | null
> {
  const conn = await getConnection();

  try {
    const result = await conn.query(`
			SELECT
				MIN("${fieldName}") as min_val,
				MAX("${fieldName}") as max_val,
				AVG(TRY_CAST("${fieldName}" AS DOUBLE)) as avg_val
			FROM data
		`);

    const row = result.toArray()[0];
    if (!row) return null;

    return {
      min: Number(row.min_val),
      max: Number(row.max_val),
      avg: Number(row.avg_val),
    };
  } catch {
    return null;
  }
}
