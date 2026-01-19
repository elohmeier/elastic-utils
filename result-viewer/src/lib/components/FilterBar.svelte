<script lang="ts">
import type { Filter } from "$lib/stores/app-state.svelte";
import { X } from "@lucide/svelte";

let {
  filters,
  onRemove,
  onClearAll,
}: {
  filters: Filter[];
  onRemove: (id: string) => void;
  onClearAll: () => void;
} = $props();

const operatorLabels: Record<Filter["operator"], string> = {
  eq: "=",
  neq: "!=",
  contains: "contains",
  gt: ">",
  lt: "<",
  gte: ">=",
  lte: "<=",
  exists: "exists",
  not_exists: "not exists",
};

function formatFilter(filter: Filter): string {
  if (filter.operator === "exists" || filter.operator === "not_exists") {
    return `${filter.field} ${operatorLabels[filter.operator]}`;
  }
  return `${filter.field} ${operatorLabels[filter.operator]} ${filter.value}`;
}
</script>

{#if filters.length > 0}
  <div class="bg-muted/50 flex flex-wrap items-center gap-2 rounded p-2">
    {#each filters as filter (filter.id)}
      <span
        class="bg-primary/10 text-primary inline-flex items-center gap-1 rounded px-2 py-1 text-sm"
      >
        {formatFilter(filter)}
        <button
          onclick={() => onRemove(filter.id)}
          class="hover:bg-primary/20 rounded p-0.5"
          title="Remove filter"
        >
          <X class="h-3 w-3" />
        </button>
      </span>
    {/each}

    <button
      class="text-muted-foreground hover:text-foreground text-sm"
      onclick={onClearAll}
    >
      Clear all
    </button>
  </div>
{/if}
