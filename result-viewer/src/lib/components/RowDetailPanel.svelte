<script lang="ts">
import { Button } from "$lib/components/ui/button";
import { Check, Copy, X } from "@lucide/svelte";

let {
  data,
  onClose,
}: {
  data: Record<string, unknown> | null;
  onClose: () => void;
} = $props();

let copied = $state(false);

function bigIntReplacer(_: string, v: unknown): unknown {
  return typeof v === "bigint" ? v.toString() : v;
}

async function copyToClipboard() {
  if (!data) return;
  try {
    await navigator.clipboard.writeText(
      JSON.stringify(data, bigIntReplacer, 2),
    );
    copied = true;
    setTimeout(() => (copied = false), 2000);
  } catch (err) {
    console.error("Failed to copy:", err);
  }
}

interface JsonNode {
  type:
    | "string"
    | "number"
    | "boolean"
    | "null"
    | "key"
    | "bracket"
    | "punctuation";
  value: string;
}

function tokenize(value: unknown, indent: number = 0): JsonNode[][] {
  const lines: JsonNode[][] = [];
  const pad = "  ".repeat(indent);

  if (value === null) {
    lines.push([{ type: "null", value: "null" }]);
  } else if (value === undefined) {
    lines.push([{ type: "null", value: "undefined" }]);
  } else if (typeof value === "bigint") {
    lines.push([{ type: "number", value: value.toString() }]);
  } else if (typeof value === "string") {
    lines.push([{ type: "string", value: `"${escapeString(value)}"` }]);
  } else if (typeof value === "number") {
    lines.push([{ type: "number", value: String(value) }]);
  } else if (typeof value === "boolean") {
    lines.push([{ type: "boolean", value: String(value) }]);
  } else if (Array.isArray(value)) {
    if (value.length === 0) {
      lines.push([{ type: "bracket", value: "[]" }]);
    } else {
      lines.push([{ type: "bracket", value: "[" }]);
      value.forEach((item, i) => {
        const itemLines = tokenize(item, indent + 1);
        itemLines.forEach((line, j) => {
          const isLast = i === value.length - 1 && j === itemLines.length - 1;
          const prefix = j === 0 ? "  ".repeat(indent + 1) : "";
          if (j === itemLines.length - 1 && !isLast) {
            lines.push([{ type: "punctuation", value: prefix }, ...line, {
              type: "punctuation",
              value: ",",
            }]);
          } else {
            lines.push([{ type: "punctuation", value: prefix }, ...line]);
          }
        });
      });
      lines.push([{ type: "bracket", value: pad + "]" }]);
    }
  } else if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) {
      lines.push([{ type: "bracket", value: "{}" }]);
    } else {
      lines.push([{ type: "bracket", value: "{" }]);
      entries.forEach(([k, v], i) => {
        const valLines = tokenize(v, indent + 1);
        const keyPart: JsonNode[] = [
          { type: "punctuation", value: "  ".repeat(indent + 1) },
          { type: "key", value: `"${k}"` },
          { type: "punctuation", value: ": " },
        ];
        if (valLines.length === 1) {
          const isLast = i === entries.length - 1;
          lines.push([...keyPart, ...valLines[0], {
            type: "punctuation",
            value: isLast ? "" : ",",
          }]);
        } else {
          lines.push([...keyPart, ...valLines[0]]);
          valLines.slice(1, -1).forEach((line) => lines.push(line));
          const lastLine = valLines[valLines.length - 1];
          const isLast = i === entries.length - 1;
          lines.push([...lastLine, {
            type: "punctuation",
            value: isLast ? "" : ",",
          }]);
        }
      });
      lines.push([{ type: "bracket", value: pad + "}" }]);
    }
  } else {
    lines.push([{ type: "string", value: String(value) }]);
  }

  return lines;
}

function escapeString(str: string): string {
  return str
    .replace(/\\/g, "\\\\")
    .replace(/"/g, "\\\"")
    .replace(/\n/g, "\\n")
    .replace(/\r/g, "\\r")
    .replace(/\t/g, "\\t");
}

const tokenizedData = $derived(data ? tokenize(data) : []);
</script>

{#if data}
  <div class="bg-muted/30 flex h-full flex-col border-t p-4">
    <div class="mb-2 flex shrink-0 items-center justify-between">
      <h3 class="text-sm font-medium">Document Details</h3>
      <div class="flex items-center gap-2">
        <Button variant="outline" size="sm" onclick={copyToClipboard}>
          {#if copied}
            <Check class="mr-1 h-4 w-4" />
            Copied
          {:else}
            <Copy class="mr-1 h-4 w-4" />
            Copy JSON
          {/if}
        </Button>
        <Button variant="ghost" size="sm" onclick={onClose}>
          <X class="h-4 w-4" />
        </Button>
      </div>
    </div>
    <pre class="bg-background flex-1 overflow-auto rounded border p-4 text-xs font-mono leading-relaxed">{#each tokenizedData as line}{#each line as token}<span class="json-{token.type}">{token.value}</span>{/each}
{/each}</pre>
  </div>
{/if}

<style>
.json-string {
	color: #22863a;
}
.json-number {
	color: #005cc5;
}
.json-boolean {
	color: #d73a49;
}
.json-null {
	color: #6a737d;
}
.json-key {
	color: #032f62;
}
.json-bracket {
	color: #24292e;
}
.json-punctuation {
	color: #24292e;
}

:global(.dark) .json-string {
	color: #85e89d;
}
:global(.dark) .json-number {
	color: #79b8ff;
}
:global(.dark) .json-boolean {
	color: #f97583;
}
:global(.dark) .json-null {
	color: #959da5;
}
:global(.dark) .json-key {
	color: #9ecbff;
}
:global(.dark) .json-bracket {
	color: #e1e4e8;
}
:global(.dark) .json-punctuation {
	color: #e1e4e8;
}
</style>
