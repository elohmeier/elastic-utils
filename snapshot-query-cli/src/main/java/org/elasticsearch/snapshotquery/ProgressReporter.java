package org.elasticsearch.snapshotquery;

import org.elasticsearch.cli.Terminal;

import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Background thread that prints in-place progress updates using ANSI escape codes.
 * Uses \r to overwrite the current line and \033[K to erase trailing characters.
 */
final class ProgressReporter implements AutoCloseable {
    private static final long PROGRESS_INTERVAL_MS = 5000L;

    private final AtomicBoolean running = new AtomicBoolean(true);
    private final AtomicBoolean progressLineActive = new AtomicBoolean(false);
    private final Terminal terminal;
    private final Thread thread;

    ProgressReporter(Terminal terminal, ProfilingRecorder profilingRecorder) {
        this.terminal = terminal;
        this.thread = new Thread(() -> {
            while (running.get()) {
                try {
                    Thread.sleep(PROGRESS_INTERVAL_MS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
                if (!running.get()) {
                    return;
                }

                int activeCount = profilingRecorder.activeShardCount();
                int shardId = profilingRecorder.currentShardId();
                int totalShards = profilingRecorder.currentTotalShards();
                String shardText;
                if (activeCount > 1) {
                    shardText = " " + activeCount + " shards active/" + totalShards;
                } else if (shardId >= 0) {
                    shardText = " shard " + (shardId + 1) + "/" + totalShards;
                } else {
                    shardText = "";
                }
                String stage = profilingRecorder.currentStage();
                long docs = profilingRecorder.totalDocsExported();
                long s3Bytes = profilingRecorder.s3BytesRead();
                long s3Calls = profilingRecorder.s3ReadFullCalls() + profilingRecorder.s3ReadRangeCalls();
                long elapsedMs = profilingRecorder.totalMillis();
                long elapsed = elapsedMs / 1000;

                long totalHits;
                if (activeCount > 1) {
                    totalHits = profilingRecorder.activeShardsTotalHits();
                } else {
                    totalHits = profilingRecorder.currentShardTotalHits();
                }
                String hitsText = totalHits > 0 ? " (" + formatNumber(totalHits) + " hits)" : "";

                double docsPerSec = elapsedMs > 0 ? docs * 1000.0 / elapsedMs : 0;
                double bytesPerSec = elapsedMs > 0 ? s3Bytes * 1000.0 / elapsedMs : 0;

                String line = profilingRecorder.completedTargets() + "/" + profilingRecorder.totalTargets()
                    + " indices"
                    + shardText
                    + " | " + stage
                    + " | " + formatNumber(docs) + " docs" + hitsText
                    + " | " + humanBytes(s3Bytes) + " from S3 (" + humanBytes((long) bytesPerSec) + "/s)"
                    + " | " + formatNumber(s3Calls) + " S3 calls"
                    + " | " + elapsed + "s";

                printProgressLine(line);
            }
        }, "snapshot-export-progress");
        this.thread.setDaemon(true);
        this.thread.start();
    }

    /**
     * Clear the in-place progress line so permanent output can be printed cleanly.
     * Call this before any terminal.errorPrintln() during export.
     */
    void clearLine() {
        if (progressLineActive.compareAndSet(true, false)) {
            terminal.errorPrint(Terminal.Verbosity.NORMAL, "\r\033[K");
        }
    }

    private void printProgressLine(String line) {
        terminal.errorPrint(Terminal.Verbosity.NORMAL, "\r" + line + "\033[K");
        progressLineActive.set(true);
    }

    @Override
    public void close() {
        running.set(false);
        thread.interrupt();
        clearLine();
    }

    static String humanBytes(long bytes) {
        double value = bytes;
        String[] units = {"B", "KiB", "MiB", "GiB", "TiB"};
        int unit = 0;
        while (value >= 1024 && unit < units.length - 1) {
            value /= 1024.0;
            unit++;
        }
        return String.format(Locale.ROOT, "%.1f%s", value, units[unit]);
    }

    private static String formatNumber(long n) {
        if (n < 1000) return Long.toString(n);
        if (n < 1_000_000) return String.format(Locale.ROOT, "%,.1fK", n / 1000.0);
        return String.format(Locale.ROOT, "%,.1fM", n / 1_000_000.0);
    }
}
