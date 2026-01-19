<script lang="ts">
import { Clock, Database, FileText } from "@lucide/svelte";

let {
  fileName,
  fileSize,
  totalRows,
  filteredRows,
  queryTime,
  offset,
  limit,
}: {
  fileName: string | null;
  fileSize: number;
  totalRows: number;
  filteredRows: number;
  queryTime: number;
  offset: number;
  limit: number;
} = $props();

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

const showingFrom = $derived(offset + 1);
const showingTo = $derived(Math.min(offset + limit, filteredRows));
</script>

<div class="bg-muted/30 text-muted-foreground flex items-center gap-4 border-t px-4 py-2 text-xs">
  {#if fileName}
    <div class="flex items-center gap-1">
      <FileText class="h-3 w-3" />
      <span>{fileName}</span>
      <span class="text-muted-foreground/60">({formatSize(fileSize)})</span>
    </div>
  {/if}

  <div class="flex items-center gap-1">
    <Database class="h-3 w-3" />
    <span>
      {#if filteredRows !== totalRows}
        {filteredRows.toLocaleString()} of {totalRows.toLocaleString()}
        documents
      {:else}
        {totalRows.toLocaleString()} documents
      {/if}
    </span>
  </div>

  {#if filteredRows > 0}
    <div class="text-muted-foreground/60">
      Showing {showingFrom.toLocaleString()}-{showingTo.toLocaleString()}
    </div>
  {/if}

  {#if queryTime > 0}
    <div class="flex items-center gap-1">
      <Clock class="h-3 w-3" />
      <span>{queryTime.toFixed(0)}ms</span>
    </div>
  {/if}
</div>
