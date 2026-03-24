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
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.Arrays;
import java.util.List;

public class ExportRangeCli extends Command {

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

        SearchBodyParser searchBody = parseSearchBody(options, fromDate, toDate);
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
        try (S3ClientFactory.S3Access s3Access = s3Options.connect(options)) {
            BlobContainer rootContainer = s3Access.rootContainer();
            SnapshotMetadataLoader metadataLoader = new SnapshotMetadataLoader(rootContainer, s3Access);

            terminal.errorPrintln("Discovering snapshot/index pairs...");
            List<SnapshotMetadataLoader.ExportTarget> targets = metadataLoader.findExportTargets(
                indexPattern,
                minIndexDate,
                maxIndexDate,
                !allSnapshots
            );

            if (targets.isEmpty()) {
                terminal.errorPrintln("No matching snapshot/index pairs found");
                return;
            }

            terminal.errorPrintln("Found " + targets.size() + " snapshot/index pairs to export");
            long totalExported = 0;
            int current = 0;

            for (SnapshotMetadataLoader.ExportTarget target : targets) {
                current++;
                String outputPath = buildOutputPath(outputDir, target, compression, allSnapshots, targets);
                terminal.errorPrintln("[" + current + "/" + targets.size() + "] Exporting " + target.indexName() + " from " + target.snapshotName() + "...");

                try (OutputStream out = SnapshotExportSupport.openOutput(outputPath, compression)) {
                    SnapshotMetadataLoader.ResolvedIndex resolved = metadataLoader.resolve(target.snapshotName(), target.indexName());
                    long exported = SnapshotExportSupport.exportResolvedIndex(
                        terminal,
                        metadataLoader,
                        resolved,
                        luceneQuery,
                        sort,
                        sourceFields,
                        batchSize,
                        out,
                        startTime
                    );
                    out.flush();
                    totalExported += exported;
                    terminal.errorPrintln("  Index complete: " + exported + " docs -> " + outputPath);
                }
            }

            long tookMs = System.currentTimeMillis() - startTime;
            terminal.errorPrintln("Export-range complete: " + totalExported + " documents in " + (tookMs / 1000) + "s");
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
}
