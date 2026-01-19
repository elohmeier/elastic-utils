import type { FieldSchema, LoadedFile } from "$lib/services/file-loader";
import { buildFieldTree, flattenFieldTree } from "$lib/services/schema-parser";

export interface Filter {
  id: string;
  field: string;
  operator: "eq" | "neq" | "contains" | "gt" | "lt" | "gte" | "lte" | "exists" | "not_exists";
  value: string | number | boolean;
}

export interface TimeRange {
  from: Date | null;
  to: Date | null;
  field: string;
}

export interface AppState {
  // File state
  loadedFile: LoadedFile | null;
  isLoading: boolean;
  error: string | null;

  // Query state
  searchQuery: string;
  filters: Filter[];
  timeRange: TimeRange;

  // View state
  visibleColumns: string[];
  sortColumn: string | null;
  sortDirection: "asc" | "desc";

  // Pagination
  offset: number;
  limit: number;
  totalRows: number;
  filteredRows: number;

  // Query stats
  lastQueryTime: number;

  // Row detail
  selectedRowIndex: number | null;
  expandedRowData: Record<string, unknown> | null;
}

function createAppState() {
  let state = $state<AppState>({
    loadedFile: null,
    isLoading: false,
    error: null,
    searchQuery: "",
    filters: [],
    timeRange: { from: null, to: null, field: "@timestamp" },
    visibleColumns: [],
    sortColumn: null,
    sortDirection: "desc",
    offset: 0,
    limit: 100,
    totalRows: 0,
    filteredRows: 0,
    lastQueryTime: 0,
    selectedRowIndex: null,
    expandedRowData: null,
  });

  return {
    get state() {
      return state;
    },

    setLoading(loading: boolean) {
      state.isLoading = loading;
    },

    setError(error: string | null) {
      state.error = error;
    },

    setLoadedFile(file: LoadedFile) {
      state.loadedFile = file;
      state.totalRows = file.rowCount;
      state.filteredRows = file.rowCount;

      // Auto-detect timestamp field - only match actual TIMESTAMP types, not STRUCTs
      const isTimestampType = (type: string) => {
        const t = type.toUpperCase();
        return (
          (t === "TIMESTAMP"
            || t === "TIMESTAMPTZ"
            || t === "TIMESTAMP WITH TIME ZONE"
            || t.startsWith("TIMESTAMP"))
          && !t.startsWith("STRUCT")
        );
      };

      // First try to find a top-level timestamp field
      let timestampField = file.schema.find(
        (f) => isTimestampType(f.type) && f.name.toLowerCase().includes("timestamp"),
      );
      // If not found, try any timestamp type
      if (!timestampField) {
        timestampField = file.schema.find((f) => isTimestampType(f.type));
      }
      // For Elasticsearch exports, check for _source which contains @timestamp
      if (!timestampField) {
        const sourceField = file.schema.find((f) => f.name === "_source");
        if (sourceField && sourceField.type.includes("@timestamp")) {
          // Use nested path syntax for DuckDB
          state.timeRange.field = "_source.\"@timestamp\"";
        }
      } else {
        state.timeRange.field = `"${timestampField.name}"`;
      }

      // Set default visible columns
      // For Elasticsearch exports, prioritize _source.@timestamp and _source.message
      const fieldTree = buildFieldTree(file.schema);
      const allLeafFields = flattenFieldTree(fieldTree);

      const defaultColumns: string[] = [];

      // Look for timestamp field in _source
      const sourceTimestampField = allLeafFields.find(
        (f) => f.displayPath === "_source.@timestamp",
      );
      if (sourceTimestampField) {
        defaultColumns.push(sourceTimestampField.path);
      }

      // Look for message field in _source
      const messageField = allLeafFields.find(
        (f) => f.displayPath === "_source.message",
      );
      if (messageField) {
        defaultColumns.push(messageField.path);
      }

      // If no preferred fields found, fall back to first 6 non-struct fields
      if (defaultColumns.length === 0) {
        for (const field of file.schema) {
          if (!field.type.toUpperCase().startsWith("STRUCT")) {
            defaultColumns.push(`"${field.name}"`);
          }
        }
        state.visibleColumns = defaultColumns.slice(0, 6);
      } else {
        state.visibleColumns = defaultColumns;
      }

      state.offset = 0;
      state.selectedRowIndex = null;
      state.expandedRowData = null;
    },

    clearFile() {
      state.loadedFile = null;
      state.totalRows = 0;
      state.filteredRows = 0;
      state.visibleColumns = [];
      state.filters = [];
      state.searchQuery = "";
      state.offset = 0;
      state.selectedRowIndex = null;
      state.expandedRowData = null;
    },

    addFilter(filter: Omit<Filter, "id">) {
      const newFilter: Filter = {
        ...filter,
        id: crypto.randomUUID(),
      };
      state.filters = [...state.filters, newFilter];
      state.offset = 0;
    },

    removeFilter(id: string) {
      state.filters = state.filters.filter((f) => f.id !== id);
      state.offset = 0;
    },

    clearFilters() {
      state.filters = [];
      state.offset = 0;
    },

    setSearchQuery(query: string) {
      state.searchQuery = query;
      state.offset = 0;
    },

    setTimeRange(from: Date | null, to: Date | null) {
      state.timeRange.from = from;
      state.timeRange.to = to;
      state.offset = 0;
    },

    setTimeRangeField(field: string) {
      state.timeRange.field = field;
      state.offset = 0;
    },

    toggleColumn(column: string) {
      if (state.visibleColumns.includes(column)) {
        state.visibleColumns = state.visibleColumns.filter((c) => c !== column);
      } else {
        state.visibleColumns = [...state.visibleColumns, column];
      }
    },

    setVisibleColumns(columns: string[]) {
      state.visibleColumns = columns;
    },

    setSort(column: string) {
      if (state.sortColumn === column) {
        state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
      } else {
        state.sortColumn = column;
        state.sortDirection = "desc";
      }
    },

    clearSort() {
      state.sortColumn = null;
      state.sortDirection = "desc";
    },

    setPage(offset: number) {
      state.offset = offset;
    },

    setLimit(limit: number) {
      state.limit = limit;
      state.offset = 0;
    },

    setFilteredRows(count: number) {
      state.filteredRows = count;
    },

    setLastQueryTime(time: number) {
      state.lastQueryTime = time;
    },

    setSelectedRow(index: number | null) {
      state.selectedRowIndex = index;
    },

    setExpandedRowData(data: Record<string, unknown> | null) {
      state.expandedRowData = data;
    },
  };
}

export const appState = createAppState();
