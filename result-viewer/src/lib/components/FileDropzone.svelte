<script lang="ts">
import { FileText, Loader2, Upload } from "@lucide/svelte";

let {
  onFileSelect,
  isLoading = false,
}: { onFileSelect: (file: File) => void; isLoading?: boolean } = $props();

let isDragging = $state(false);
let fileInput: HTMLInputElement;

function handleDrop(e: DragEvent) {
  e.preventDefault();
  isDragging = false;
  const file = e.dataTransfer?.files[0];
  if (file && (file.name.endsWith(".jsonl") || file.name.endsWith(".ndjson"))) {
    onFileSelect(file);
  }
}

function handleFileInput(e: Event) {
  const target = e.target as HTMLInputElement;
  const file = target.files?.[0];
  if (file) onFileSelect(file);
}

function handleClick() {
  if (!isLoading) {
    fileInput.click();
  }
}

function handleKeyDown(e: KeyboardEvent) {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    handleClick();
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}
</script>

<div
  class="
    flex h-64 cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-8 text-center transition-colors
    {isDragging ? 'border-primary bg-primary/5' : 'border-muted-foreground/25 hover:border-muted-foreground/50'}
    {isLoading ? 'cursor-wait opacity-75' : ''}
  "
  ondragover={(e) => {
    e.preventDefault();
    isDragging = true;
  }}
  ondragleave={() => (isDragging = false)}
  ondrop={handleDrop}
  onclick={handleClick}
  onkeydown={handleKeyDown}
  role="button"
  tabindex="0"
>
  {#if isLoading}
    <Loader2 class="text-muted-foreground mb-4 h-12 w-12 animate-spin" />
    <p class="text-lg font-medium">Loading file...</p>
    <p class="text-muted-foreground text-sm">
      This may take a moment for large files
    </p>
  {:else}
    <Upload class="text-muted-foreground mb-4 h-12 w-12" />
    <p class="text-lg font-medium">Drop JSONL file here</p>
    <p class="text-muted-foreground text-sm">or click to browse</p>
    <p class="text-muted-foreground mt-2 text-xs">
      Supports .jsonl and .ndjson files
    </p>
  {/if}

  <input
    bind:this={fileInput}
    type="file"
    accept=".jsonl,.ndjson"
    class="hidden"
    onchange={handleFileInput}
    disabled={isLoading}
  />
</div>
