<script lang="ts">
import type { FieldSchema } from "$lib/services/file-loader";
import { Clock, X } from "@lucide/svelte";

let {
  from = $bindable<Date | null>(null),
  to = $bindable<Date | null>(null),
  field,
  onFieldChange,
  onChange,
  schema = [],
}: {
  from: Date | null;
  to: Date | null;
  field: string;
  onFieldChange: (field: string) => void;
  onChange: (from: Date | null, to: Date | null) => void;
  schema: FieldSchema[];
} = $props();

const presets = [
  { label: "Last 15 minutes", ms: 15 * 60 * 1000 },
  { label: "Last 1 hour", ms: 60 * 60 * 1000 },
  { label: "Last 24 hours", ms: 24 * 60 * 60 * 1000 },
  { label: "Last 7 days", ms: 7 * 24 * 60 * 60 * 1000 },
  { label: "Last 30 days", ms: 30 * 24 * 60 * 60 * 1000 },
];

let selectedPreset = $state<number | null>(null);

function applyPreset(ms: number) {
  selectedPreset = ms;
  to = new Date();
  from = new Date(Date.now() - ms);
  onChange(from, to);
}

function clearTimeRange() {
  selectedPreset = null;
  from = null;
  to = null;
  onChange(null, null);
}

function formatDateForInput(date: Date | null): string {
  if (!date) return "";
  return date.toISOString().slice(0, 16);
}

function handleFromChange(e: Event) {
  const target = e.target as HTMLInputElement;
  from = target.value ? new Date(target.value) : null;
  selectedPreset = null;
  onChange(from, to);
}

function handleToChange(e: Event) {
  const target = e.target as HTMLInputElement;
  to = target.value ? new Date(target.value) : null;
  selectedPreset = null;
  onChange(from, to);
}

// Build list of timestamp field options
const timestampFields = $derived(() => {
  const fields: Array<{ value: string; label: string }> = [];

  for (const f of schema) {
    const typeUpper = f.type.toUpperCase();
    // Direct timestamp type
    if (
      (typeUpper === "TIMESTAMP"
        || typeUpper === "TIMESTAMPTZ"
        || typeUpper.startsWith("TIMESTAMP"))
      && !typeUpper.startsWith("STRUCT")
    ) {
      fields.push({ value: f.name, label: f.name });
    }
    // Check for nested @timestamp in _source (Elasticsearch pattern)
    if (f.name === "_source" && f.type.includes("@timestamp")) {
      fields.push({
        value: "_source.\"@timestamp\"",
        label: "_source.@timestamp",
      });
    }
  }

  return fields;
});
</script>

<div class="flex flex-wrap items-center gap-2">
  <Clock class="text-muted-foreground h-4 w-4" />

  {#if timestampFields().length > 0}
    <select
      class="border-input bg-background rounded border px-2 py-1 text-sm"
      value={field}
      onchange={(e) => onFieldChange((e.target as HTMLSelectElement).value)}
    >
      {#each timestampFields() as f}
        <option value={f.value}>{f.label}</option>
      {/each}
    </select>
  {/if}

  <select
    class="border-input bg-background rounded border px-2 py-1 text-sm"
    value={selectedPreset ?? ""}
    onchange={(e) => {
      const val = (e.target as HTMLSelectElement).value;
      if (val) applyPreset(Number(val));
    }}
  >
    <option value="">Custom range...</option>
    {#each presets as preset}
      <option value={preset.ms}>{preset.label}</option>
    {/each}
  </select>

  <input
    type="datetime-local"
    class="border-input bg-background rounded border px-2 py-1 text-sm"
    value={formatDateForInput(from)}
    onchange={handleFromChange}
  />
  <span class="text-muted-foreground text-sm">to</span>
  <input
    type="datetime-local"
    class="border-input bg-background rounded border px-2 py-1 text-sm"
    value={formatDateForInput(to)}
    onchange={handleToChange}
  />

  {#if from || to}
    <button
      onclick={clearTimeRange}
      class="text-muted-foreground hover:text-foreground"
      title="Clear time range"
    >
      <X class="h-4 w-4" />
    </button>
  {/if}
</div>
