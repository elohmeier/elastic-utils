package org.elasticsearch.snapshotquery;

import com.github.luben.zstd.ZstdOutputStream;

import joptsimple.OptionSet;
import joptsimple.OptionSpec;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.StoredFields;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.Sort;
import org.apache.lucene.search.TopDocs;
import org.apache.lucene.search.TopFieldDocs;
import org.apache.lucene.store.Directory;
import org.elasticsearch.cli.Command;
import org.elasticsearch.cli.ExitCodes;
import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.cli.UserException;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.core.PathUtils;
import org.elasticsearch.core.SuppressForbidden;
import org.elasticsearch.index.snapshots.blobstore.BlobStoreIndexShardSnapshot;
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentParserConfiguration;
import org.elasticsearch.xcontent.XContentType;

import java.io.BufferedOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public class SnapshotExportCli extends Command {

    private final OptionSpec<String> bucketOption;
    private final OptionSpec<String> basePathOption;
    private final OptionSpec<String> regionOption;
    private final OptionSpec<String> endpointOption;
    private final OptionSpec<String> accessKeyOption;
    private final OptionSpec<String> secretKeyOption;
    private final OptionSpec<Void> trustAllCertsOption;
    private final OptionSpec<String> resolveOption;
    private final OptionSpec<String> snapshotOption;
    private final OptionSpec<String> indexOption;
    private final OptionSpec<String> queryFileOption;
    private final OptionSpec<String> queryOption;
    private final OptionSpec<String> fromDateOption;
    private final OptionSpec<String> toDateOption;
    private final OptionSpec<String> outputOption;
    private final OptionSpec<String> compressionOption;
    private final OptionSpec<Integer> batchSizeOption;

    public SnapshotExportCli() {
        super("Export documents from Elasticsearch snapshots in S3 to JSONL");
        bucketOption = parser.acceptsAll(Arrays.asList("b", "bucket"), "S3 bucket name").withRequiredArg().required();
        basePathOption = parser.accepts("base-path", "Base path within the S3 bucket").withRequiredArg().defaultsTo("");
        regionOption = parser.accepts("region", "AWS region").withRequiredArg().defaultsTo("us-east-1");
        endpointOption = parser.accepts("endpoint", "Custom S3 endpoint URL").withRequiredArg();
        accessKeyOption = parser.accepts("access-key", "AWS access key").withRequiredArg();
        secretKeyOption = parser.accepts("secret-key", "AWS secret key").withRequiredArg();
        trustAllCertsOption = parser.accepts("trust-all-certs", "Disable TLS certificate verification");
        resolveOption = parser.accepts("resolve", "Resolve hostname to IP (hostname:ip)").withRequiredArg();
        snapshotOption = parser.acceptsAll(Arrays.asList("s", "snapshot"), "Snapshot name (auto-detected if omitted)").withRequiredArg();
        indexOption = parser.acceptsAll(Arrays.asList("i", "index"), "Index name, alias, or pattern").withRequiredArg().required();
        queryFileOption = parser.accepts("query-file", "Path to JSON file containing search body or Query DSL").withRequiredArg();
        queryOption = parser.acceptsAll(Arrays.asList("q", "query"), "Inline Query DSL JSON").withRequiredArg();
        fromDateOption = parser.accepts("from-date", "Start date filter for @timestamp (inclusive, ISO date or datetime)").withRequiredArg();
        toDateOption = parser.accepts("to-date", "End date filter for @timestamp (exclusive, ISO date or datetime)").withRequiredArg();
        outputOption = parser.acceptsAll(Arrays.asList("o", "output"), "Output file path (default: stdout)").withRequiredArg();
        compressionOption = parser.accepts("compression", "Compression: none, zstd").withRequiredArg().defaultsTo("none");
        batchSizeOption = parser.accepts("batch-size", "Documents per search batch").withRequiredArg().ofType(Integer.class).defaultsTo(10000);
    }

    @Override
    protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo) throws Exception {
        long startTime = System.currentTimeMillis();

        String bucket = bucketOption.value(options);
        String basePath = basePathOption.value(options);
        String region = regionOption.value(options);
        String endpoint = options.has(endpointOption) ? endpointOption.value(options) : null;
        String accessKey = options.has(accessKeyOption) ? accessKeyOption.value(options) : null;
        String secretKey = options.has(secretKeyOption) ? secretKeyOption.value(options) : null;
        boolean trustAllCerts = options.has(trustAllCertsOption);
        String resolve = options.has(resolveOption) ? resolveOption.value(options) : null;
        String snapshotName = options.has(snapshotOption) ? snapshotOption.value(options) : null;
        String indexNameOrAlias = indexOption.value(options);
        String fromDate = options.has(fromDateOption) ? fromDateOption.value(options) : null;
        String toDate = options.has(toDateOption) ? toDateOption.value(options) : null;
        String outputPath = options.has(outputOption) ? outputOption.value(options) : null;
        String compression = compressionOption.value(options);
        int batchSize = batchSizeOption.value(options);

        // Parse search body
        SearchBodyParser searchBody = parseSearchBody(options, fromDate, toDate);
        Query luceneQuery = searchBody.query();
        Sort sort = searchBody.sort();
        List<String> sourceFields = searchBody.sourceFields();

        terminal.errorPrintln("Connecting to S3 bucket: " + bucket);

        try (
            S3ClientFactory.S3Access s3Access = S3ClientFactory.create(bucket, basePath, region, endpoint, accessKey, secretKey, trustAllCerts, resolve);
            OutputStream out = openOutput(outputPath, compression)
        ) {
            BlobContainer rootContainer = s3Access.rootContainer();
            SnapshotMetadataLoader metadataLoader = new SnapshotMetadataLoader(rootContainer, s3Access);

            // Auto-discover snapshot if not specified
            if (snapshotName == null) {
                terminal.errorPrintln("No snapshot specified, searching for snapshots containing [" + indexNameOrAlias + "]...");
                java.util.List<String> candidates = metadataLoader.findSnapshotsForIndex(indexNameOrAlias);
                if (candidates.isEmpty()) {
                    throw new UserException(ExitCodes.CONFIG, "No snapshots found containing index/alias [" + indexNameOrAlias + "]");
                }
                snapshotName = candidates.get(0); // newest first
                terminal.errorPrintln("Auto-selected snapshot: " + snapshotName);
                if (candidates.size() > 1) {
                    terminal.errorPrintln("  (other candidates: " + String.join(", ", candidates.subList(1, Math.min(5, candidates.size())))
                        + (candidates.size() > 5 ? " ..." : "") + ")");
                }
            }

            terminal.errorPrintln("Snapshot: " + snapshotName + ", Index: " + indexNameOrAlias);
            if (fromDate != null || toDate != null) {
                terminal.errorPrintln("Date range: " + (fromDate != null ? fromDate : "*") + " to " + (toDate != null ? toDate : "*"));
            }

            // Resolve index (supports aliases and patterns)
            terminal.errorPrintln("Resolving indices...");
            List<SnapshotMetadataLoader.ResolvedIndex> resolvedIndices = metadataLoader.resolveIndices(snapshotName, indexNameOrAlias);
            terminal.errorPrintln("Found " + resolvedIndices.size() + " index/indices");

            long totalExported = 0;

            for (SnapshotMetadataLoader.ResolvedIndex resolved : resolvedIndices) {
                String indexName = resolved.indexId().getName();
                int shardCount = resolved.shardSnapshots().size();
                terminal.errorPrintln("Processing index [" + indexName + "] with " + shardCount + " shard(s)...");

                // Export each shard
                for (SnapshotMetadataLoader.ShardSnapshot shardSnapshot : resolved.shardSnapshots()) {
                    int shardId = shardSnapshot.shardId();
                    BlobContainer shardContainer = metadataLoader.shardContainer(resolved.indexId(), shardId);
                    BlobStoreIndexShardSnapshot blobStoreSnapshot = shardSnapshot.snapshot();

                    try (
                        Directory directory = new SnapshotQueryDirectory(shardContainer, blobStoreSnapshot);
                        DirectoryReader reader = DirectoryReader.open(directory)
                    ) {
                        IndexSearcher searcher = new IndexSearcher(reader);
                        long shardExported = exportShard(searcher, luceneQuery, sort, sourceFields, batchSize, out);
                        totalExported += shardExported;

                        if (shardExported > 0) {
                            long elapsed = (System.currentTimeMillis() - startTime) / 1000;
                            terminal.errorPrintln("  Shard " + shardId + ": exported " + shardExported
                                + " docs (total: " + totalExported + ", elapsed: " + elapsed + "s)");
                        }
                    }
                }
            }

            out.flush();
            long tookMs = System.currentTimeMillis() - startTime;
            terminal.errorPrintln("Export complete: " + totalExported + " documents in " + (tookMs / 1000) + "s");
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

    private static boolean isFullSearchBody(String json) throws IOException {
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

    @SuppressForbidden(reason = "file output for cli")
    private static OutputStream openOutput(String path, String compression) throws IOException {
        OutputStream base;
        if (path != null) {
            base = new BufferedOutputStream(Files.newOutputStream(PathUtils.get(path)), 256 * 1024);
        } else {
            base = new BufferedOutputStream(System.out, 256 * 1024);
        }

        return switch (compression.toLowerCase()) {
            case "zstd" -> new ZstdOutputStream(base, 3); // level 3 = good balance
            case "none", "" -> base;
            default -> throw new IllegalArgumentException("Unsupported compression: " + compression);
        };
    }

    /**
     * Export all matching documents from a single shard using searchAfter pagination.
     */
    private static long exportShard(
        IndexSearcher searcher,
        Query query,
        Sort sort,
        List<String> sourceFields,
        int batchSize,
        OutputStream out
    ) throws IOException {
        long exported = 0;
        ScoreDoc lastDoc = null;
        Set<String> fieldSet = sourceFields != null ? new HashSet<>(sourceFields) : null;

        while (true) {
            TopDocs topDocs;
            if (lastDoc == null) {
                topDocs = searcher.search(query, batchSize, sort);
            } else {
                topDocs = searcher.searchAfter(lastDoc, query, batchSize, sort);
            }

            if (topDocs.scoreDocs.length == 0) {
                break;
            }

            StoredFields storedFields = searcher.storedFields();

            for (ScoreDoc scoreDoc : topDocs.scoreDocs) {
                var doc = storedFields.document(scoreDoc.doc);
                var sourceField = doc.getBinaryValue("_source");
                if (sourceField != null) {
                    byte[] sourceBytes = filterSource(sourceField.bytes, sourceField.offset, sourceField.length, fieldSet);
                    out.write(sourceBytes);
                    out.write('\n');
                    exported++;
                }
            }

            lastDoc = topDocs.scoreDocs[topDocs.scoreDocs.length - 1];
        }

        return exported;
    }

    /**
     * Filter _source JSON to only include requested fields. Returns compact single-line JSON.
     */
    private static byte[] filterSource(byte[] bytes, int offset, int length, Set<String> fields) throws IOException {
        if (fields == null || fields.isEmpty()) {
            // No filtering, but ensure it's single-line
            return compactJson(bytes, offset, length);
        }

        try (XContentParser parser = XContentType.JSON.xContent().createParser(XContentParserConfiguration.EMPTY, bytes, offset, length)) {
            parser.nextToken(); // START_OBJECT
            Map<String, Object> filtered = new HashMap<>();
            while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
                String fieldName = parser.currentName();
                parser.nextToken();
                if (fields.contains(fieldName)) {
                    filtered.put(fieldName, parseValue(parser));
                } else {
                    parser.skipChildren();
                }
            }

            // Serialize to compact JSON
            StringBuilder sb = new StringBuilder();
            sb.append('{');
            boolean first = true;
            for (var entry : filtered.entrySet()) {
                if (!first) sb.append(',');
                sb.append('"').append(escapeJson(entry.getKey())).append("\":");
                appendValue(sb, entry.getValue());
                first = false;
            }
            sb.append('}');
            return sb.toString().getBytes(StandardCharsets.UTF_8);
        }
    }

    private static byte[] compactJson(byte[] bytes, int offset, int length) {
        // Fast path: check if already single-line (no newlines)
        for (int i = offset; i < offset + length; i++) {
            if (bytes[i] == '\n' || bytes[i] == '\r') {
                // Contains newlines, need to compact
                byte[] result = new byte[length];
                int pos = 0;
                boolean inString = false;
                boolean escaped = false;
                for (int j = offset; j < offset + length; j++) {
                    byte b = bytes[j];
                    if (escaped) {
                        result[pos++] = b;
                        escaped = false;
                    } else if (b == '\\' && inString) {
                        result[pos++] = b;
                        escaped = true;
                    } else if (b == '"') {
                        result[pos++] = b;
                        inString = !inString;
                    } else if (!inString && (b == '\n' || b == '\r' || b == '\t' || b == ' ')) {
                        // skip whitespace outside strings
                    } else {
                        result[pos++] = b;
                    }
                }
                return Arrays.copyOf(result, pos);
            }
        }
        // Already single-line
        if (offset == 0 && length == bytes.length) {
            return bytes;
        }
        return Arrays.copyOfRange(bytes, offset, offset + length);
    }

    @SuppressWarnings("unchecked")
    private static Object parseValue(XContentParser parser) throws IOException {
        return switch (parser.currentToken()) {
            case VALUE_STRING -> parser.text();
            case VALUE_NUMBER -> parser.numberValue();
            case VALUE_BOOLEAN -> parser.booleanValue();
            case VALUE_NULL -> null;
            case START_OBJECT -> {
                Map<String, Object> map = new HashMap<>();
                while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
                    String key = parser.currentName();
                    parser.nextToken();
                    map.put(key, parseValue(parser));
                }
                yield map;
            }
            case START_ARRAY -> {
                List<Object> list = new ArrayList<>();
                while (parser.nextToken() != XContentParser.Token.END_ARRAY) {
                    list.add(parseValue(parser));
                }
                yield list;
            }
            default -> parser.text();
        };
    }

    @SuppressWarnings("unchecked")
    private static void appendValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            sb.append('"').append(escapeJson(s)).append('"');
        } else if (value instanceof Number n) {
            sb.append(n);
        } else if (value instanceof Boolean b) {
            sb.append(b);
        } else if (value instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (var entry : ((Map<String, Object>) map).entrySet()) {
                if (!first) sb.append(',');
                sb.append('"').append(escapeJson(entry.getKey())).append("\":");
                appendValue(sb, entry.getValue());
                first = false;
            }
            sb.append('}');
        } else if (value instanceof List<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) sb.append(',');
                appendValue(sb, item);
                first = false;
            }
            sb.append(']');
        }
    }

    private static String escapeJson(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t");
    }
}
