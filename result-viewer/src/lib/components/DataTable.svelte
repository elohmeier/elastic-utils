<script lang="ts">
import * as Table from "$lib/components/ui/table";
import { getResultColumnName } from "$lib/services/query-builder";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight } from "@lucide/svelte";

let {
  data,
  visibleColumns,
  sortColumn,
  sortDirection,
  onSort,
  onRowClick,
  selectedRowIndex,
}: {
  data: Record<string, unknown>[];
  visibleColumns: string[]; // Field paths like "_source"."message"
  sortColumn: string | null;
  sortDirection: "asc" | "desc";
  onSort: (column: string) => void;
  onRowClick: (index: number) => void;
  selectedRowIndex: number | null;
} = $props();

// Convert path to display name for column header
function pathToDisplayName(path: string): string {
  // Remove quotes and convert to readable format
  return path.replace(/"/g, "").replace(/\./g, ".");
}

function formatCellValue(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "bigint") {
    return value.toString();
  }
  // Try to format as date if it looks like a timestamp
  if (typeof value === "string" || typeof value === "number") {
    // Check if it looks like an ISO date string
    if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value)) {
      try {
        const date = new Date(value);
        if (!isNaN(date.getTime())) {
          return date.toLocaleString();
        }
      } catch {
        // Fall through
      }
    }
  }
  if (typeof value === "object") {
    // Handle BigInt in JSON stringify
    const str = JSON.stringify(
      value,
      (_, v) => (typeof v === "bigint" ? v.toString() : v),
    );
    return str.length > 100 ? str.slice(0, 100) + "..." : str;
  }
  const str = String(value);
  return str.length > 100 ? str.slice(0, 100) + "..." : str;
}

// Get the value from the row using the result column name
function getRowValue(row: Record<string, unknown>, path: string): unknown {
  const resultColumnName = getResultColumnName(path);
  return row[resultColumnName];
}
</script>

<div class="overflow-auto rounded-md border">
  <Table.Root>
    <Table.Header>
      <Table.Row>
        <Table.Head class="w-8"></Table.Head>
        {#each visibleColumns as columnPath}
          <Table.Head
            class="cursor-pointer select-none whitespace-nowrap hover:bg-muted/50"
            onclick={() => onSort(columnPath)}
          >
            <div class="flex items-center gap-1">
              <span
                class="truncate max-w-[200px]"
                title={pathToDisplayName(columnPath)}
              >
                {pathToDisplayName(columnPath)}
              </span>
              {#if sortColumn === columnPath}
                {#if sortDirection === "asc"}
                  <ArrowUp class="h-4 w-4 shrink-0" />
                {:else}
                  <ArrowDown class="h-4 w-4 shrink-0" />
                {/if}
              {/if}
            </div>
          </Table.Head>
        {/each}
      </Table.Row>
    </Table.Header>
    <Table.Body>
      {#each data as row, index}
        <Table.Row
          class="cursor-pointer {selectedRowIndex === index ? 'bg-muted' : 'hover:bg-muted/50'}"
          onclick={() => onRowClick(index)}
        >
          <Table.Cell class="w-8 p-2">
            {#if selectedRowIndex === index}
              <ChevronDown class="h-4 w-4" />
            {:else}
              <ChevronRight class="h-4 w-4" />
            {/if}
          </Table.Cell>
          {#each visibleColumns as columnPath}
            <Table.Cell class="max-w-xs truncate">
              {formatCellValue(getRowValue(row, columnPath))}
            </Table.Cell>
          {/each}
        </Table.Row>
      {/each}
      {#if data.length === 0}
        <Table.Row>
          <Table.Cell
            colspan={visibleColumns.length + 1}
            class="py-8 text-center text-muted-foreground"
          >
            No data to display
          </Table.Cell>
        </Table.Row>
      {/if}
    </Table.Body>
  </Table.Root>
</div>
