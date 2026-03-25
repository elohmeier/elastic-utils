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
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentParserConfiguration;
import org.elasticsearch.xcontent.XContentType;

import java.io.IOException;
import java.io.OutputStream;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.List;

public class SnapshotExportCli extends Command {

    private final S3Options s3Options;
    private final OptionSpec<String> snapshotOption;
    private final OptionSpec<String> indexOption;
    private final OptionSpec<String> queryFileOption;
    private final OptionSpec<String> queryOption;
    private final OptionSpec<String> fromDateOption;
    private final OptionSpec<String> toDateOption;
    private final OptionSpec<String> outputOption;
    private final OptionSpec<String> compressionOption;
    private final OptionSpec<Integer> batchSizeOption;
    private final OptionSpec<String> profileFileOption;

    public SnapshotExportCli() {
        super("Export documents from Elasticsearch snapshots in S3 to JSONL");
        s3Options = new S3Options(parser);
        snapshotOption = parser.acceptsAll(Arrays.asList("s", "snapshot"), "Snapshot name (auto-detected if omitted)").withRequiredArg();
        indexOption = parser.acceptsAll(Arrays.asList("i", "index"), "Index name, alias, or pattern").withRequiredArg().required();
        queryFileOption = parser.accepts("query-file", "Path to JSON file containing search body or Query DSL").withRequiredArg();
        queryOption = parser.acceptsAll(Arrays.asList("q", "query"), "Inline Query DSL JSON").withRequiredArg();
        fromDateOption = parser.accepts("from-date", "Start date filter for @timestamp (inclusive, ISO date or datetime)").withRequiredArg();
        toDateOption = parser.accepts("to-date", "End date filter for @timestamp (exclusive, ISO date or datetime)").withRequiredArg();
        outputOption = parser.acceptsAll(Arrays.asList("o", "output"), "Output file path (default: stdout)").withRequiredArg();
        compressionOption = parser.accepts("compression", "Compression: none, zstd").withRequiredArg().defaultsTo("none");
        batchSizeOption = parser.accepts("batch-size", "Documents per search batch").withRequiredArg().ofType(Integer.class).defaultsTo(10000);
        profileFileOption = parser.accepts("profile-file", "Write JSON profiling counters to this file").withRequiredArg();
    }

    @Override
    protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo) throws Exception {
        long startTime = System.currentTimeMillis();

        String snapshotName = options.has(snapshotOption) ? snapshotOption.value(options) : null;
        String indexNameOrAlias = indexOption.value(options);
        String fromDate = options.has(fromDateOption) ? fromDateOption.value(options) : null;
        String toDate = options.has(toDateOption) ? toDateOption.value(options) : null;
        String outputPath = options.has(outputOption) ? outputOption.value(options) : null;
        String compression = compressionOption.value(options);
        int batchSize = batchSizeOption.value(options);
        String profileFile = options.has(profileFileOption) ? profileFileOption.value(options) : null;

        // Parse search body
        SearchBodyParser searchBody = parseSearchBody(options, fromDate, toDate);
        Query luceneQuery = searchBody.query();
        Sort sort = searchBody.sort();
        List<String> sourceFields = searchBody.sourceFields();

        ProfilingRecorder profilingRecorder = new ProfilingRecorder();

        terminal.errorPrintln("Connecting to S3 bucket: " + s3Options.bucket(options));

        try (
            S3ClientFactory.S3Access s3Access = s3Options.connect(options, profilingRecorder);
            OutputStream out = SnapshotExportSupport.openOutput(outputPath, compression);
            ProgressReporter progressReporter = new ProgressReporter(terminal, profilingRecorder)
        ) {
            BlobContainer rootContainer = s3Access.rootContainer();
            SnapshotMetadataLoader metadataLoader = new SnapshotMetadataLoader(rootContainer, s3Access);

            // Auto-discover snapshot if not specified
            if (snapshotName == null) {
                progressReporter.clearLine();
                terminal.errorPrintln("No snapshot specified, searching for snapshots containing [" + indexNameOrAlias + "]...");
                java.util.List<String> candidates = metadataLoader.findSnapshotsForIndex(indexNameOrAlias);
                if (candidates.isEmpty()) {
                    throw new UserException(ExitCodes.CONFIG, "No snapshots found containing index/alias [" + indexNameOrAlias + "]");
                }
                snapshotName = candidates.get(0); // newest first
                progressReporter.clearLine();
                terminal.errorPrintln("Auto-selected snapshot: " + snapshotName);
                if (candidates.size() > 1) {
                    progressReporter.clearLine();
                    terminal.errorPrintln("  (other candidates: " + String.join(", ", candidates.subList(1, Math.min(5, candidates.size())))
                        + (candidates.size() > 5 ? " ..." : "") + ")");
                }
            }

            progressReporter.clearLine();
            terminal.errorPrintln("Snapshot: " + snapshotName + ", Index: " + indexNameOrAlias);
            if (fromDate != null || toDate != null) {
                progressReporter.clearLine();
                terminal.errorPrintln("Date range: " + (fromDate != null ? fromDate : "*") + " to " + (toDate != null ? toDate : "*"));
            }

            // Resolve index (supports aliases and patterns)
            profilingRecorder.setCurrentStage("resolving");
            progressReporter.clearLine();
            terminal.errorPrintln("Resolving indices...");
            List<SnapshotMetadataLoader.ResolvedIndex> resolvedIndices = metadataLoader.resolveIndices(snapshotName, indexNameOrAlias);
            profilingRecorder.setTargetTotals(0, resolvedIndices.size());
            progressReporter.clearLine();
            terminal.errorPrintln("Found " + resolvedIndices.size() + " index/indices");

            long totalExported = 0;
            int current = 0;

            for (SnapshotMetadataLoader.ResolvedIndex resolved : resolvedIndices) {
                current++;
                profilingRecorder.startTarget(snapshotName, resolved.indexId().getName(), current - 1, resolvedIndices.size());
                totalExported += SnapshotExportSupport.exportResolvedIndex(
                    terminal,
                    metadataLoader,
                    resolved,
                    luceneQuery,
                    sort,
                    sourceFields,
                    batchSize,
                    out,
                    startTime,
                    profilingRecorder,
                    progressReporter
                );
                profilingRecorder.finishTarget(current, resolvedIndices.size());
            }

            out.flush();
            long tookMs = System.currentTimeMillis() - startTime;
            progressReporter.clearLine();
            terminal.errorPrintln("Export complete: " + totalExported + " documents in " + (tookMs / 1000) + "s");
        }

        if (profileFile != null) {
            profilingRecorder.markCompleted();
            writeProfileFile(profileFile, profilingRecorder);
            terminal.errorPrintln("Profile written to " + profileFile);
        }
    }

    @SuppressForbidden(reason = "write profile file for cli")
    private static void writeProfileFile(String path, ProfilingRecorder recorder) throws IOException {
        java.nio.file.Path outputPath = PathUtils.get(path);
        java.nio.file.Path parent = outputPath.getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        java.nio.file.Path tempFile = outputPath.resolveSibling(outputPath.getFileName() + ".tmp");
        Files.writeString(tempFile, recorder.renderJson());
        try {
            Files.move(tempFile, outputPath, java.nio.file.StandardCopyOption.REPLACE_EXISTING, java.nio.file.StandardCopyOption.ATOMIC_MOVE);
        } catch (java.nio.file.AtomicMoveNotSupportedException e) {
            Files.move(tempFile, outputPath, java.nio.file.StandardCopyOption.REPLACE_EXISTING);
        }
    }

    private SearchBodyParser parseSearchBody(OptionSet options, String fromDate, String toDate) throws UserException, IOException {
        String json;
        if (options.has(queryFileOption)) {
            json = readFile(queryFileOption.value(options));
        } else if (options.has(queryOption)) {
            json = queryOption.value(options);
        } else {
            throw new UserException(ExitCodes.USAGE, "Either --query or --query-file must be specified");
        }

        // Detect if it's a full search body (has top-level "query" key) or just a query
        if (isFullSearchBody(json)) {
            return SearchBodyParser.parse(json, fromDate, toDate);
        } else {
            return SearchBodyParser.fromQueryOnly(json, fromDate, toDate);
        }
    }

    static boolean isFullSearchBody(String json) throws IOException {
        try (XContentParser parser = XContentType.JSON.xContent().createParser(XContentParserConfiguration.EMPTY, json)) {
            parser.nextToken(); // START_OBJECT
            while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
                String field = parser.currentName();
                if ("query".equals(field) || "sort".equals(field) || "_source".equals(field) || "track_total_hits".equals(field)) {
                    return true;
                }
                parser.nextToken();
                parser.skipChildren();
            }
        }
        return false;
    }

    @SuppressForbidden(reason = "file arg for cli")
    private static String readFile(String path) throws IOException {
        return Files.readString(PathUtils.get(path));
    }

}
