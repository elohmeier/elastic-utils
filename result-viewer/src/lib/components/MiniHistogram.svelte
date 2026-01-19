<script lang="ts">
let {
  data,
}: {
  data: Array<{ value: string; count: number }>;
} = $props();

const maxCount = $derived(Math.max(...data.map((d) => d.count), 1));
</script>

<div class="flex h-8 items-end gap-px">
  {#each data as bar, i}
    <div
      class="bg-primary/60 hover:bg-primary min-w-[4px] flex-1 transition-colors"
      style="height: {(bar.count / maxCount) * 100}%"
      title="{bar.value}: {bar.count.toLocaleString()}"
    >
    </div>
  {/each}
</div>
