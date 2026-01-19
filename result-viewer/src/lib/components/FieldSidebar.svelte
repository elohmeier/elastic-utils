<script lang="ts">
import {
  type FieldStatistics,
  getFieldStatistics,
} from "$lib/services/field-stats";
import type { FieldSchema } from "$lib/services/file-loader";
import {
  buildFieldTree,
  getBaseType,
  type NestedField,
} from "$lib/services/schema-parser";
import {
  ChevronDown,
  ChevronRight,
  Columns,
  Eye,
  EyeOff,
  Plus,
} from "@lucide/svelte";
import MiniHistogram from "./MiniHistogram.svelte";

let {
  fields,
  visibleColumns,
  onToggleColumn,
  onAddFilter,
}: {
  fields: FieldSchema[];
  visibleColumns: string[];
  onToggleColumn: (fieldPath: string) => void;
  onAddFilter: (field: string, operator: string, value: string) => void;
} = $props();

// Build nested field tree from schema
const fieldTree = $derived(buildFieldTree(fields));

let expandedFields = $state<Set<string>>(new Set());
let fieldStats = $state<Map<string, FieldStatistics>>(new Map());
let loadingStats = $state<Set<string>>(new Set());

function toggleExpand(path: string) {
  if (expandedFields.has(path)) {
    expandedFields.delete(path);
  } else {
    expandedFields.add(path);
  }
  expandedFields = new Set(expandedFields);
}

async function loadStats(field: NestedField) {
  if (fieldStats.has(field.path) || loadingStats.has(field.path)) return;

  // Don't load stats for STRUCT types
  if (getBaseType(field.type) === "STRUCT") return;

  loadingStats.add(field.path);
  loadingStats = new Set(loadingStats);

  try {
    const stats = await getFieldStatistics(field.path);
    fieldStats.set(field.path, stats);
    fieldStats = new Map(fieldStats);
  } catch (err) {
    console.error(`Failed to load stats for ${field.path}:`, err);
  } finally {
    loadingStats.delete(field.path);
    loadingStats = new Set(loadingStats);
  }
}

function getTypeColor(type: string): string {
  const base = getBaseType(type).toUpperCase();
  if (
    base.includes("INT") || base.includes("DOUBLE") || base.includes("FLOAT")
    || base.includes("DECIMAL")
  ) {
    return "text-blue-600 dark:text-blue-400";
  }
  if (base.includes("VARCHAR") || base.includes("STRING")) {
    return "text-green-600 dark:text-green-400";
  }
  if (
    base.includes("TIMESTAMP") || base.includes("DATE") || base.includes("TIME")
  ) {
    return "text-purple-600 dark:text-purple-400";
  }
  if (base.includes("BOOL")) {
    return "text-orange-600 dark:text-orange-400";
  }
  if (base === "STRUCT" || base === "MAP" || base === "LIST") {
    return "text-yellow-600 dark:text-yellow-400";
  }
  return "text-muted-foreground";
}

function getShortType(type: string): string {
  const base = getBaseType(type).toUpperCase();
  if (base === "STRUCT") return "obj";
  if (base === "LIST") return "arr";
  if (base === "MAP") return "map";
  if (base.includes("VARCHAR")) return "str";
  if (base.includes("BIGINT")) return "i64";
  if (base.includes("INTEGER") || base.includes("INT")) return "int";
  if (base.includes("DOUBLE")) return "f64";
  if (base.includes("FLOAT")) return "f32";
  if (base.includes("TIMESTAMP")) return "time";
  if (base.includes("BOOLEAN")) return "bool";
  return base.slice(0, 4).toLowerCase();
}

function isColumnVisible(path: string): boolean {
  return visibleColumns.includes(path);
}

function handleExpandAndLoadStats(field: NestedField) {
  toggleExpand(field.path);
  if (!field.children || field.children.length === 0) {
    loadStats(field);
  }
}
</script>

<!-- Recursive field component -->
{#snippet fieldItem(field: NestedField, depth: number = 0)}
  {@const hasChildren = field.children && field.children.length > 0}
  {@const isExpanded = expandedFields.has(field.path)}
  {@const baseType = getBaseType(field.type)}
  {@const isLeaf = !hasChildren}

  <div class="mb-0.5" style="padding-left: {depth * 12}px">
    <div class="hover:bg-accent flex items-center gap-1 rounded px-2 py-1">
      <!-- Expand/collapse button -->
      <button
        class="flex flex-1 items-center gap-1 text-left min-w-0"
        onclick={() => handleExpandAndLoadStats(field)}
      >
        {#if hasChildren}
          {#if isExpanded}
            <ChevronDown class="h-4 w-4 shrink-0" />
          {:else}
            <ChevronRight class="h-4 w-4 shrink-0" />
          {/if}
        {:else}
          <span class="w-4 shrink-0"></span>
        {/if}

        <span class="flex-1 truncate text-sm" title={field.displayPath}>{
          field.name
        }</span>
        <span
          class="text-xs shrink-0 {getTypeColor(field.type)}"
          title={field.type}
        >
          {getShortType(field.type)}
        </span>
      </button>

      <!-- Add to columns button (only for leaf fields) -->
      {#if isLeaf}
        <button
          onclick={() => onToggleColumn(field.path)}
          class="hover:bg-background shrink-0 rounded p-1"
          title={isColumnVisible(field.path) ? "Hide column" : "Show column"}
        >
          {#if isColumnVisible(field.path)}
            <Eye class="h-4 w-4" />
          {:else}
            <EyeOff class="text-muted-foreground h-4 w-4" />
          {/if}
        </button>
      {:else}
        <!-- For STRUCT fields, add all children button -->
        <button
          onclick={() => {
            if (field.children) {
              for (const child of field.children) {
                if (!child.children) {
                  onToggleColumn(child.path);
                }
              }
            }
          }}
          class="hover:bg-background shrink-0 rounded p-1"
          title="Toggle child columns"
        >
          <Columns class="h-4 w-4 text-muted-foreground" />
        </button>
      {/if}
    </div>

    <!-- Expanded content: children or stats -->
    {#if isExpanded}
      {#if hasChildren}
        <!-- Render children recursively -->
        {#each field.children as child}
          {@render fieldItem(child, depth + 1)}
        {/each}
      {:else}
        <!-- Show field stats for leaf nodes -->
        <div class="ml-5 mt-1 mr-2 rounded border p-2">
          {#if loadingStats.has(field.path)}
            <p class="text-muted-foreground text-xs">Loading statistics...</p>
          {:else if fieldStats.has(field.path)}
            {@const stats = fieldStats.get(field.path)!}
            <div class="space-y-2">
              <div class="text-muted-foreground flex justify-between text-xs">
                <span>Unique: {stats.uniqueCount.toLocaleString()}</span>
                <span>Null: {stats.nullCount.toLocaleString()}</span>
              </div>

              {#if stats.topValues.length > 0}
                <MiniHistogram data={stats.topValues} />

                <div class="mt-2 space-y-1">
                  {#each stats.topValues.slice(0, 5) as item}
                    <button
                      class="hover:bg-accent flex w-full items-center gap-1 rounded px-1 py-0.5 text-left text-xs"
                      onclick={() => onAddFilter(field.path, "eq", item.value)}
                      title="Add filter: {field.displayPath} = {item.value}"
                    >
                      <Plus class="h-3 w-3 shrink-0" />
                      <span class="flex-1 truncate">{
                        item.value || "(empty)"
                      }</span>
                      <span class="text-muted-foreground">{
                        item.count.toLocaleString()
                      }</span>
                    </button>
                  {/each}
                </div>
              {/if}
            </div>
          {:else}
            <p class="text-muted-foreground text-xs">
              Click to load statistics
            </p>
          {/if}
        </div>
      {/if}
    {/if}
  </div>
{/snippet}

<div class="flex h-full flex-col border-r">
  <div class="border-b p-3">
    <h3 class="text-sm font-semibold">Fields ({fields.length})</h3>
  </div>

  <div class="flex-1 overflow-y-auto p-2">
    {#each fieldTree as field}
      {@render fieldItem(field, 0)}
    {/each}
  </div>
</div>
