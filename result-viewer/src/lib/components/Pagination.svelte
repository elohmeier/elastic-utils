<script lang="ts">
import { Button } from "$lib/components/ui/button";
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
} from "@lucide/svelte";

let {
  offset,
  limit,
  total,
  onPageChange,
  onLimitChange,
}: {
  offset: number;
  limit: number;
  total: number;
  onPageChange: (newOffset: number) => void;
  onLimitChange: (newLimit: number) => void;
} = $props();

const currentPage = $derived(Math.floor(offset / limit) + 1);
const totalPages = $derived(Math.ceil(total / limit));
const canGoPrev = $derived(offset > 0);
const canGoNext = $derived(offset + limit < total);

function goToPage(page: number) {
  const newOffset = (page - 1) * limit;
  onPageChange(Math.max(0, Math.min(newOffset, (totalPages - 1) * limit)));
}

function goFirst() {
  onPageChange(0);
}

function goPrev() {
  onPageChange(Math.max(0, offset - limit));
}

function goNext() {
  onPageChange(Math.min((totalPages - 1) * limit, offset + limit));
}

function goLast() {
  onPageChange((totalPages - 1) * limit);
}
</script>

<div class="flex items-center justify-between px-4 py-2">
  <div class="flex items-center gap-2">
    <span class="text-muted-foreground text-sm">Rows per page:</span>
    <select
      class="border-input bg-background rounded border px-2 py-1 text-sm"
      value={limit}
      onchange={(e) => onLimitChange(Number((e.target as HTMLSelectElement).value))}
    >
      <option value={25}>25</option>
      <option value={50}>50</option>
      <option value={100}>100</option>
      <option value={250}>250</option>
      <option value={500}>500</option>
    </select>
  </div>

  <div class="flex items-center gap-1">
    <Button
      variant="outline"
      size="icon"
      onclick={goFirst}
      disabled={!canGoPrev}
    >
      <ChevronsLeft class="h-4 w-4" />
    </Button>
    <Button
      variant="outline"
      size="icon"
      onclick={goPrev}
      disabled={!canGoPrev}
    >
      <ChevronLeft class="h-4 w-4" />
    </Button>

    <span class="text-muted-foreground px-2 text-sm">
      Page {currentPage} of {totalPages}
    </span>

    <Button
      variant="outline"
      size="icon"
      onclick={goNext}
      disabled={!canGoNext}
    >
      <ChevronRight class="h-4 w-4" />
    </Button>
    <Button
      variant="outline"
      size="icon"
      onclick={goLast}
      disabled={!canGoNext}
    >
      <ChevronsRight class="h-4 w-4" />
    </Button>
  </div>
</div>
