package org.elasticsearch.snapshotquery;

import joptsimple.OptionSet;
import joptsimple.OptionSpec;

import org.apache.lucene.search.Query;
import org.apache.lucene.search.Sort;
import org.elasticsearch.cli.Command;
import org.elasticsearch.cli.ExitCodes;
import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.cli.UserException;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.core.PathUtils;
import org.elasticsearch.core.SuppressForbidden;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

public class ExportRangeCli extends Command {
    private static final long PROGRESS_INTERVAL_MS = 5000L;

    private final S3Options s3Options;
    private final OptionSpec<String> indexPatternOption;
    private final OptionSpec<String> indexDateFromOption;
    private final OptionSpec<String> indexDateToOption;
    private final OptionSpec<String> queryFileOption;
    private final OptionSpec<String> queryOption;
    private final OptionSpec<String> fromDateOption;
    private final OptionSpec<String> toDateOption;
    private final OptionSpec<String> outputDirOption;
    private final OptionSpec<String> compressionOption;
    private final OptionSpec<Integer> batchSizeOption;
    private final OptionSpec<Void> allSnapshotsOption;
    private final OptionSpec<String> profileFileOption;

    public ExportRangeCli() {
        super("Export many snapshot/index pairs for an index date range in a single JVM");
        s3Options = new S3Options(parser);
        indexPatternOption = parser.accepts("index-pattern", "Index wildcard pattern to export").withRequiredArg().required();
        indexDateFromOption = parser.accepts("index-date-from", "Inclusive index date filter (YYYY-MM-DD)").withRequiredArg().required();
        indexDateToOption = parser.accepts("index-date-to", "Inclusive index date filter (YYYY-MM-DD)").withRequiredArg().required();
        queryFileOption = parser.accepts("query-file", "Path to JSON file containing search body or Query DSL").withRequiredArg();
        queryOption = parser.acceptsAll(Arrays.asList("q", "query"), "Inline Query DSL JSON").withRequiredArg();
        fromDateOption = parser.accepts("from-date", "Start date filter for @timestamp (inclusive, ISO date or datetime)").withRequiredArg();
        toDateOption = parser.accepts("to-date", "End date filter for @timestamp (exclusive, ISO date or datetime)").withRequiredArg();
        outputDirOption = parser.accepts("output-dir", "Directory for exported files").withRequiredArg().required();
        compressionOption = parser.accepts("compression", "Compression: none, zstd").withRequiredArg().defaultsTo("none");
        batchSizeOption = parser.accepts("batch-size", "Documents per search batch").withRequiredArg().ofType(Integer.class).defaultsTo(10000);
        allSnapshotsOption = parser.accepts("all-snapshots", "Export all matching snapshot/index pairs instead of only the newest snapshot per index");
        profileFileOption = parser.accepts("profile-file", "Write JSON profiling counters to this file").withRequiredArg();
    }

    @Override
    protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo) throws Exception {
        long startTime = System.currentTimeMillis();

        String indexPattern = indexPatternOption.value(options);
        String indexDateFrom = indexDateFromOption.value(options);
        String indexDateTo = indexDateToOption.value(options);
        String fromDate = options.has(fromDateOption) ? fromDateOption.value(options) : null;
        String toDate = options.has(toDateOption) ? toDateOption.value(options) : null;
        String outputDir = outputDirOption.value(options);
        String compression = compressionOption.value(options);
        int batchSize = batchSizeOption.value(options);
        boolean allSnapshots = options.has(allSnapshotsOption);
        String profileFile = options.has(profileFileOption) ? profileFileOption.value(options) : null;
        ProfilingRecorder profilingRecorder = new ProfilingRecorder();
        ProfileFileWriter profileFileWriter = ProfileFileWriter.create(profileFile, profilingRecorder);
        ProgressReporter progressReporter = new ProgressReporter(terminal, profilingRecorder);

        long queryParseStartNanos = System.nanoTime();
        SearchBodyParser searchBody = parseSearchBody(options, fromDate, toDate);
        profilingRecorder.addPhaseNanos("query_parse", System.nanoTime() - queryParseStartNanos);
        Query luceneQuery = searchBody.query();
        Sort sort = searchBody.sort();
        List<String> sourceFields = searchBody.sourceFields();

        LocalDate minIndexDate = parseIndexDate(indexDateFromOption.value(options));
        LocalDate maxIndexDate = parseIndexDate(indexDateToOption.value(options));
        if (maxIndexDate.isBefore(minIndexDate)) {
            throw new UserException(ExitCodes.USAGE, "--index-date-to must be on or after --index-date-from");
        }

        ensureDirectory(outputDir);

        terminal.errorPrintln("Connecting to S3 bucket: " + s3Options.bucket(options));
        long connectStartNanos = System.nanoTime();
        try (
            S3ClientFactory.S3Access s3Access = s3Options.connect(options, profilingRecorder);
            ProgressReporter ignored = progressReporter
        ) {
            profilingRecorder.addPhaseNanos("s3_connect", System.nanoTime() - connectStartNanos);
            BlobContainer rootContainer = s3Access.rootContainer();
            SnapshotMetadataLoader metadataLoader = new SnapshotMetadataLoader(rootContainer, s3Access);

            terminal.errorPrintln("Discovering snapshot/index pairs...");
            profilingRecorder.setCurrentStage("discovering");
            long discoveryStartNanos = System.nanoTime();
            List<SnapshotMetadataLoader.ExportTarget> targets = metadataLoader.findExportTargets(
                indexPattern,
                minIndexDate,
                maxIndexDate,
                !allSnapshots
            );
            profilingRecorder.addPhaseNanos("target_discovery", System.nanoTime() - discoveryStartNanos);

            if (targets.isEmpty()) {
                terminal.errorPrintln("No matching snapshot/index pairs found");
                profileFileWriter.flushCompleted();
                return;
            }

            terminal.errorPrintln("Found " + targets.size() + " snapshot/index pairs to export");
            long totalExported = 0;
            int current = 0;
            profilingRecorder.setTargetTotals(0, targets.size());

            for (SnapshotMetadataLoader.ExportTarget target : targets) {
                current++;
                profilingRecorder.startTarget(target.snapshotName(), target.indexName(), current - 1, targets.size());
                String outputPath = buildOutputPath(outputDir, target, compression, allSnapshots, targets);
                terminal.errorPrintln("[" + current + "/" + targets.size() + "] Exporting " + target.indexName() + " from " + target.snapshotName() + "...");

                try (OutputStream out = SnapshotExportSupport.openOutput(outputPath, compression)) {
                    long resolveStartNanos = System.nanoTime();
                    SnapshotMetadataLoader.ResolvedIndex resolved = metadataLoader.resolve(target.snapshotName(), target.indexName());
                    profilingRecorder.recordIndexResolve(target.indexName(), target.snapshotName(), System.nanoTime() - resolveStartNanos);
                    long exportStartNanos = System.nanoTime();
                    long exported = SnapshotExportSupport.exportResolvedIndex(
                        terminal,
                        metadataLoader,
                        resolved,
                        luceneQuery,
                        sort,
                        sourceFields,
                        batchSize,
                        out,
                        startTime,
                        profilingRecorder
                    );
                    out.flush();
                    profilingRecorder.recordIndexExport(
                        target.indexName(),
                        target.snapshotName(),
                        outputPath,
                        exported,
                        System.nanoTime() - exportStartNanos
                    );
                    totalExported += exported;
                    profilingRecorder.finishTarget(current, targets.size());
                    terminal.errorPrintln("  Index complete: " + exported + " docs -> " + outputPath);
                }
            }

            long tookMs = System.currentTimeMillis() - startTime;
            terminal.errorPrintln("Export-range complete: " + totalExported + " documents in " + (tookMs / 1000) + "s");
            profileFileWriter.flushCompleted();
        }
    }

    private SearchBodyParser parseSearchBody(OptionSet options, String fromDate, String toDate) throws IOException, UserException {
        String json;
        if (options.has(queryFileOption)) {
            json = readFile(queryFileOption.value(options));
        } else if (options.has(queryOption)) {
            json = queryOption.value(options);
        } else {
            throw new UserException(ExitCodes.USAGE, "Either --query or --query-file must be specified");
        }

        if (SnapshotExportCli.isFullSearchBody(json)) {
            return SearchBodyParser.parse(json, fromDate, toDate);
        }
        return SearchBodyParser.fromQueryOnly(json, fromDate, toDate);
    }

    private static LocalDate parseIndexDate(String value) {
        try {
            return LocalDate.parse(value, DateTimeFormatter.ISO_LOCAL_DATE);
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException("Invalid index date: " + value + ". Expected YYYY-MM-DD");
        }
    }

    private static String buildOutputPath(
        String outputDir,
        SnapshotMetadataLoader.ExportTarget target,
        String compression,
        boolean allSnapshots,
        List<SnapshotMetadataLoader.ExportTarget> allTargets
    ) {
        boolean duplicateIndexName = allTargets.stream().filter(t -> t.indexName().equals(target.indexName())).count() > 1;
        String baseName = target.indexName();
        if (allSnapshots || duplicateIndexName) {
            baseName = baseName + "__" + target.snapshotName();
        }
        String extension = "zstd".equalsIgnoreCase(compression) ? ".jsonl.zst" : ".jsonl";
        return PathUtils.get(outputDir, sanitizeFileName(baseName) + extension).toString();
    }

    private static String sanitizeFileName(String value) {
        return value.replace('/', '_');
    }

    @SuppressForbidden(reason = "file arg for cli")
    private static String readFile(String path) throws IOException {
        return Files.readString(PathUtils.get(path));
    }

    @SuppressForbidden(reason = "create output directory for cli")
    private static void ensureDirectory(String outputDir) throws IOException {
        Path path = PathUtils.get(outputDir);
        Files.createDirectories(path);
    }

    private static final class ProfileFileWriter {
        private final Path outputPath;
        private final ProfilingRecorder profilingRecorder;
        private final AtomicBoolean flushed = new AtomicBoolean();
        private final Thread shutdownHook;

        private ProfileFileWriter(Path outputPath, ProfilingRecorder profilingRecorder) {
            this.outputPath = outputPath;
            this.profilingRecorder = profilingRecorder;
            this.shutdownHook = new Thread(() -> {
                try {
                    flushInterrupted();
                } catch (IOException ignored) {
                    // best effort during shutdown
                }
            }, "snapshot-query-profile-shutdown-hook");
        }

        static ProfileFileWriter create(String profileFile, ProfilingRecorder profilingRecorder) {
            if (profileFile == null || profilingRecorder == null) {
                return new ProfileFileWriter(null, null);
            }
            ProfileFileWriter writer = new ProfileFileWriter(PathUtils.get(profileFile), profilingRecorder);
            Runtime.getRuntime().addShutdownHook(writer.shutdownHook);
            return writer;
        }

        void flushCompleted() throws IOException {
            if (profilingRecorder == null || outputPath == null || !flushed.compareAndSet(false, true)) {
                return;
            }
            profilingRecorder.markCompleted();
            writeAtomically();
            removeShutdownHook();
        }

        void flushInterrupted() throws IOException {
            if (profilingRecorder == null || outputPath == null || !flushed.compareAndSet(false, true)) {
                return;
            }
            profilingRecorder.markInterrupted();
            writeAtomically();
        }

        @SuppressForbidden(reason = "write profile file for cli")
        private void writeAtomically() throws IOException {
            Path parent = outputPath.getParent();
            if (parent != null) {
                Files.createDirectories(parent);
            }
            Path tempFile = outputPath.resolveSibling(outputPath.getFileName() + ".tmp");
            Files.writeString(tempFile, profilingRecorder.renderJson());
            try {
                Files.move(tempFile, outputPath, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(tempFile, outputPath, StandardCopyOption.REPLACE_EXISTING);
            }
        }

        private void removeShutdownHook() {
            try {
                Runtime.getRuntime().removeShutdownHook(shutdownHook);
            } catch (IllegalStateException ignored) {
                // JVM is already shutting down
            }
        }
    }

    private static final class ProgressReporter implements AutoCloseable {
        private final AtomicBoolean running = new AtomicBoolean(true);
        private final Thread thread;

        private ProgressReporter(Terminal terminal, ProfilingRecorder profilingRecorder) {
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

                    String indexName = profilingRecorder.currentIndexName();
                    String snapshotName = profilingRecorder.currentSnapshotName();
                    int shardId = profilingRecorder.currentShardId();
                    String shardText = shardId >= 0 ? " shard=" + shardId : "";
                    terminal.errorPrintln(
                        "Progress: "
                            + profilingRecorder.completedTargets() + "/" + profilingRecorder.totalTargets()
                            + " indices complete"
                            + (indexName != null ? ", index=" + indexName : "")
                            + (snapshotName != null ? ", snapshot=" + snapshotName : "")
                            + shardText
                            + ", stage=" + profilingRecorder.currentStage()
                            + ", docs=" + profilingRecorder.totalDocsExported()
                            + ", s3_bytes=" + humanBytes(profilingRecorder.s3BytesRead())
                            + ", s3_calls(full/range)=" + profilingRecorder.s3ReadFullCalls() + "/" + profilingRecorder.s3ReadRangeCalls()
                            + ", elapsed=" + (profilingRecorder.totalMillis() / 1000) + "s"
                    );
                }
            }, "snapshot-export-progress");
            this.thread.setDaemon(true);
            this.thread.start();
        }

        @Override
        public void close() {
            running.set(false);
            thread.interrupt();
        }

        private static String humanBytes(long bytes) {
            double value = bytes;
            String[] units = { "B", "KiB", "MiB", "GiB", "TiB" };
            int unit = 0;
            while (value >= 1024 && unit < units.length - 1) {
                value /= 1024.0;
                unit++;
            }
            return String.format(java.util.Locale.ROOT, "%.1f%s", value, units[unit]);
        }
    }
}
