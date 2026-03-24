package org.elasticsearch.snapshotquery;

import com.github.luben.zstd.ZstdOutputStream;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.StoredFields;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.Sort;
import org.apache.lucene.search.TopDocs;
import org.apache.lucene.store.Directory;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.core.PathUtils;
import org.elasticsearch.core.SuppressForbidden;
import org.elasticsearch.index.snapshots.blobstore.BlobStoreIndexShardSnapshot;
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentParserConfiguration;
import org.elasticsearch.xcontent.XContentType;

import java.io.BufferedOutputStream;
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

final class SnapshotExportSupport {

    private SnapshotExportSupport() {}

    static long exportResolvedIndex(
        Terminal terminal,
        SnapshotMetadataLoader metadataLoader,
        SnapshotMetadataLoader.ResolvedIndex resolved,
        Query luceneQuery,
        Sort sort,
        List<String> sourceFields,
        int batchSize,
        OutputStream out,
        long startTimeMillis
    ) throws IOException {
        String indexName = resolved.indexId().getName();
        int shardCount = resolved.shardSnapshots().size();
        terminal.errorPrintln("Processing index [" + indexName + "] with " + shardCount + " shard(s)...");

        long totalExported = 0;
        for (SnapshotMetadataLoader.ShardSnapshot shardSnapshot : resolved.shardSnapshots()) {
            int shardId = shardSnapshot.shardId();
            BlobContainer shardContainer = metadataLoader.shardContainer(resolved.indexId(), shardId);
            BlobStoreIndexShardSnapshot blobStoreSnapshot = shardSnapshot.snapshot();

            try (
                Directory directory = new SnapshotQueryDirectory(shardContainer, blobStoreSnapshot);
                DirectoryReader reader = DirectoryReader.open(directory)
            ) {
                IndexSearcher searcher = new IndexSearcher(reader);
                long shardStart = System.currentTimeMillis();
                long shardExported = exportShard(searcher, luceneQuery, sort, sourceFields, batchSize, out);
                long shardTookMs = System.currentTimeMillis() - shardStart;
                totalExported += shardExported;

                long elapsed = (System.currentTimeMillis() - startTimeMillis) / 1000;
                terminal.errorPrintln(
                    "  Shard " + shardId + ": exported " + shardExported
                        + " docs (index total: " + totalExported + ", shard: " + shardTookMs + "ms, elapsed: " + elapsed + "s)"
                );
            }
        }

        return totalExported;
    }

    @SuppressForbidden(reason = "file output for cli")
    static OutputStream openOutput(String path, String compression) throws IOException {
        OutputStream base;
        if (path != null) {
            base = new BufferedOutputStream(Files.newOutputStream(PathUtils.get(path)), 256 * 1024);
        } else {
            base = new BufferedOutputStream(System.out, 256 * 1024);
        }

        return switch (compression.toLowerCase()) {
            case "zstd" -> new ZstdOutputStream(base, 3);
            case "none", "" -> base;
            default -> throw new IllegalArgumentException("Unsupported compression: " + compression);
        };
    }

    static long exportShard(
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

    private static byte[] filterSource(byte[] bytes, int offset, int length, Set<String> fields) throws IOException {
        if (fields == null || fields.isEmpty()) {
            return compactJson(bytes, offset, length);
        }

        try (XContentParser parser = XContentType.JSON.xContent().createParser(XContentParserConfiguration.EMPTY, bytes, offset, length)) {
            parser.nextToken();
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
        for (int i = offset; i < offset + length; i++) {
            if (bytes[i] == '\n' || bytes[i] == '\r') {
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
