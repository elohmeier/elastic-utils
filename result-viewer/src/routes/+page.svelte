<script lang="ts">
import DataTable from "$lib/components/DataTable.svelte";
import FieldSidebar from "$lib/components/FieldSidebar.svelte";
import FileDropzone from "$lib/components/FileDropzone.svelte";
import FilterBar from "$lib/components/FilterBar.svelte";
import Pagination from "$lib/components/Pagination.svelte";
import QueryBar from "$lib/components/QueryBar.svelte";
import RowDetailPanel from "$lib/components/RowDetailPanel.svelte";
import StatusBar from "$lib/components/StatusBar.svelte";
import TimeRangePicker from "$lib/components/TimeRangePicker.svelte";
import { Button } from "$lib/components/ui/button";
import {
  getRowById,
  loadJSONLFile,
  queryData,
} from "$lib/services/file-loader";
import { buildCountQuery, buildDataQuery } from "$lib/services/query-builder";
import { appState } from "$lib/stores/app-state.svelte";
import { Database, RefreshCw, X } from "@lucide/svelte";
import { onMount } from "svelte";

let tableData = $state<Record<string, unknown>[]>([]);
let isQuerying = $state(false);

async function handleFileSelect(file: File) {
  appState.setLoading(true);
  appState.setError(null);

  try {
    const loaded = await loadJSONLFile(file);
    appState.setLoadedFile(loaded);
    await refreshData();
  } catch (err) {
    appState.setError(
      err instanceof Error ? err.message : "Failed to load file",
    );
    console.error("File load error:", err);
  } finally {
    appState.setLoading(false);
  }
}

async function refreshData() {
  if (!appState.state.loadedFile) return;

  isQuerying = true;
  try {
    const allColumns = appState.state.loadedFile.schema.map((f) => f.name);

    // Get filtered count
    const countSql = buildCountQuery({
      searchQuery: appState.state.searchQuery,
      filters: appState.state.filters,
      timeRange: appState.state.timeRange,
      allColumns,
    });
    const countResult = await queryData(countSql);
    const filteredCount = Number(countResult.rows[0]?.cnt ?? 0);
    appState.setFilteredRows(filteredCount);

    // Get data
    const dataSql = buildDataQuery(
      {
        searchQuery: appState.state.searchQuery,
        filters: appState.state.filters,
        timeRange: appState.state.timeRange,
        visibleColumns: appState.state.visibleColumns,
        sortColumn: appState.state.sortColumn,
        sortDirection: appState.state.sortDirection,
        offset: appState.state.offset,
        limit: appState.state.limit,
      },
      allColumns,
    );
    const result = await queryData(dataSql);
    tableData = result.rows;
    appState.setLastQueryTime(result.queryTime);
  } catch (err) {
    appState.setError(err instanceof Error ? err.message : "Query failed");
    console.error("Query error:", err);
  } finally {
    isQuerying = false;
  }
}

async function handleRowClick(index: number) {
  if (appState.state.selectedRowIndex === index) {
    appState.setSelectedRow(null);
    appState.setExpandedRowData(null);
  } else {
    appState.setSelectedRow(index);
    // Get full row data
    const actualIndex = appState.state.offset + index;
    const fullRow = await getRowById(actualIndex);
    appState.setExpandedRowData(fullRow);
  }
}

function handleSearch(query: string) {
  appState.setSearchQuery(query);
  refreshData();
}

function handleAddFilter(field: string, operator: string, value: string) {
  appState.addFilter({
    field,
    operator: operator as
      | "eq"
      | "neq"
      | "contains"
      | "gt"
      | "lt"
      | "gte"
      | "lte"
      | "exists",
    value,
  });
  refreshData();
}

function handleRemoveFilter(id: string) {
  appState.removeFilter(id);
  refreshData();
}

function handleClearFilters() {
  appState.clearFilters();
  refreshData();
}

function handleSort(column: string) {
  appState.setSort(column);
  refreshData();
}

function handleTimeRangeChange(from: Date | null, to: Date | null) {
  appState.setTimeRange(from, to);
  refreshData();
}

function handleTimeRangeFieldChange(field: string) {
  appState.setTimeRangeField(field);
  if (appState.state.timeRange.from || appState.state.timeRange.to) {
    refreshData();
  }
}

function handlePageChange(offset: number) {
  appState.setPage(offset);
  refreshData();
}

function handleLimitChange(limit: number) {
  appState.setLimit(limit);
  refreshData();
}

function handleCloseFile() {
  appState.clearFile();
  tableData = [];
}
</script>

<svelte:head>
  <title>JSONL Viewer - Elasticsearch Export Analyzer</title>
</svelte:head>

<div class="flex h-screen flex-col">
  <!-- Header -->
  <header class="flex items-center justify-between border-b px-4 py-2">
    <div class="flex items-center gap-2">
      <Database class="h-5 w-5" />
      <h1 class="text-lg font-semibold">JSONL Viewer</h1>
    </div>

    {#if appState.state.loadedFile}
      <div class="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onclick={refreshData}
          disabled={isQuerying}
        >
          <RefreshCw class="mr-1 h-4 w-4 {isQuerying ? 'animate-spin' : ''}" />
          Refresh
        </Button>
        <Button variant="ghost" size="sm" onclick={handleCloseFile}>
          <X class="mr-1 h-4 w-4" />
          Close
        </Button>
      </div>
    {/if}
  </header>

  {#if appState.state.error}
    <div class="bg-destructive/10 text-destructive border-destructive/20 border-b px-4 py-2">
      {appState.state.error}
      <button class="ml-2 underline" onclick={() => appState.setError(null)}>
        Dismiss
      </button>
    </div>
  {/if}

  {#if !appState.state.loadedFile}
    <!-- File upload view -->
    <div class="flex flex-1 items-center justify-center p-8">
      <div class="w-full max-w-xl">
        <FileDropzone
          onFileSelect={handleFileSelect}
          isLoading={appState.state.isLoading}
        />
        <p class="text-muted-foreground mt-4 text-center text-sm">
          Upload an Elasticsearch JSONL export to analyze it in your browser.
          <br />
          All processing happens locally - no data is sent to any server.
        </p>
      </div>
    </div>
  {:else}
    <!-- Main viewer -->
    <div class="flex flex-1 overflow-hidden">
      <!-- Sidebar -->
      <div class="w-64 shrink-0 overflow-hidden">
        <FieldSidebar
          fields={appState.state.loadedFile.schema}
          visibleColumns={appState.state.visibleColumns}
          onToggleColumn={(col) => {
            appState.toggleColumn(col);
            refreshData();
          }}
          onAddFilter={handleAddFilter}
        />
      </div>

      <!-- Main content -->
      <div class="flex flex-1 flex-col overflow-hidden">
        <!-- Query controls -->
        <div class="space-y-2 border-b p-4">
          <QueryBar
            value={appState.state.searchQuery}
            onSearch={handleSearch}
          />

          <TimeRangePicker
            from={appState.state.timeRange.from}
            to={appState.state.timeRange.to}
            field={appState.state.timeRange.field}
            onFieldChange={handleTimeRangeFieldChange}
            onChange={handleTimeRangeChange}
            schema={appState.state.loadedFile.schema}
          />

          <FilterBar
            filters={appState.state.filters}
            onRemove={handleRemoveFilter}
            onClearAll={handleClearFilters}
          />
        </div>

        <!-- Data table -->
        <div class="flex-1 overflow-auto p-4">
          <DataTable
            data={tableData}
            visibleColumns={appState.state.visibleColumns}
            sortColumn={appState.state.sortColumn}
            sortDirection={appState.state.sortDirection}
            onSort={handleSort}
            onRowClick={handleRowClick}
            selectedRowIndex={appState.state.selectedRowIndex}
          />
        </div>

        <!-- Row detail panel -->
        {#if appState.state.expandedRowData}
          <RowDetailPanel
            data={appState.state.expandedRowData}
            onClose={() => {
              appState.setSelectedRow(null);
              appState.setExpandedRowData(null);
            }}
          />
        {/if}

        <!-- Pagination -->
        <Pagination
          offset={appState.state.offset}
          limit={appState.state.limit}
          total={appState.state.filteredRows}
          onPageChange={handlePageChange}
          onLimitChange={handleLimitChange}
        />
      </div>
    </div>

    <!-- Status bar -->
    <StatusBar
      fileName={appState.state.loadedFile.name}
      fileSize={appState.state.loadedFile.size}
      totalRows={appState.state.totalRows}
      filteredRows={appState.state.filteredRows}
      queryTime={appState.state.lastQueryTime}
      offset={appState.state.offset}
      limit={appState.state.limit}
    />
  {/if}
</div>
