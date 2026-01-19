<script lang="ts">
import { Button } from "$lib/components/ui/button";
import { Search, X } from "@lucide/svelte";

let {
  value = $bindable(""),
  onSearch,
  placeholder = "Search across all fields...",
}: {
  value: string;
  onSearch: (query: string) => void;
  placeholder?: string;
} = $props();

let inputValue = $state(value);
let debounceTimer: ReturnType<typeof setTimeout> | null = null;

function handleInput(e: Event) {
  const target = e.target as HTMLInputElement;
  inputValue = target.value;

  // Debounce search
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    onSearch(inputValue);
  }, 300);
}

function handleSubmit(e: Event) {
  e.preventDefault();
  if (debounceTimer) clearTimeout(debounceTimer);
  onSearch(inputValue);
}

function handleClear() {
  inputValue = "";
  onSearch("");
}
</script>

<form onsubmit={handleSubmit} class="flex gap-2">
  <div class="relative flex-1">
    <Search
      class="text-muted-foreground absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2"
    />
    <input
      type="text"
      bind:value={inputValue}
      oninput={handleInput}
      {placeholder}
      class="bg-background border-input focus:ring-ring w-full rounded-md border py-2 pl-10 pr-10 text-sm focus:outline-none focus:ring-2"
    />
    {#if inputValue}
      <button
        type="button"
        onclick={handleClear}
        class="text-muted-foreground hover:text-foreground absolute right-3 top-1/2 -translate-y-1/2"
      >
        <X class="h-4 w-4" />
      </button>
    {/if}
  </div>
  <Button type="submit" size="sm">Search</Button>
</form>
