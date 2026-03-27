package org.elasticsearch.snapshotquery;

import com.github.luben.zstd.ZstdOutputStream;
import java.io.BufferedOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
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
      long startTimeMillis,
      ProfilingRecorder profilingRecorder)
      throws IOException {
    return exportResolvedIndex(
        terminal,
        metadataLoader,
        resolved,
        luceneQuery,
        sort,
        sourceFields,
        batchSize,
        out,
        startTimeMillis,
        profilingRecorder,
        null,
        1);
  }

  static long exportResolvedIndex(
      Terminal terminal,
      SnapshotMetadataLoader metadataLoader,
      SnapshotMetadataLoader.ResolvedIndex resolved,
      Query luceneQuery,
      Sort sort,
      List<String> sourceFields,
      int batchSize,
      OutputStream out,
      long startTimeMillis,
      ProfilingRecorder profilingRecorder,
      ProgressReporter progressReporter)
      throws IOException {
    return exportResolvedIndex(
        terminal,
        metadataLoader,
        resolved,
        luceneQuery,
        sort,
        sourceFields,
        batchSize,
        out,
        startTimeMillis,
        profilingRecorder,
        progressReporter,
        1);
  }

  static long exportResolvedIndex(
      Terminal terminal,
      SnapshotMetadataLoader metadataLoader,
      SnapshotMetadataLoader.ResolvedIndex resolved,
      Query luceneQuery,
      Sort sort,
      List<String> sourceFields,
      int batchSize,
      OutputStream out,
      long startTimeMillis,
      ProfilingRecorder profilingRecorder,
      ProgressReporter progressReporter,
      int parallelShards)
      throws IOException {
    String indexName = resolved.indexId().getName();
    int shardCount = resolved.shardSnapshots().size();
    if (progressReporter != null) progressReporter.clearLine();
    terminal.errorPrintln(
        "Processing index [" + indexName + "] with " + shardCount + " shard(s)...");

    if (parallelShards > 1 && shardCount > 1) {
      return exportShardsParallel(
          terminal,
          metadataLoader,
          resolved,
          luceneQuery,
          sort,
          sourceFields,
          batchSize,
          out,
          startTimeMillis,
          profilingRecorder,
          progressReporter,
          Math.min(parallelShards, shardCount));
    }

    long totalExported = 0;
    for (SnapshotMetadataLoader.ShardSnapshot shardSnapshot : resolved.shardSnapshots()) {
      int shardId = shardSnapshot.shardId();
      BlobContainer shardContainer = metadataLoader.shardContainer(resolved.indexId(), shardId);
      BlobStoreIndexShardSnapshot blobStoreSnapshot = shardSnapshot.snapshot();
      if (profilingRecorder != null) {
        profilingRecorder.startShard(indexName, shardId, shardCount, "opening");
      }

      long openStartNanos = System.nanoTime();
      try (Directory directory =
              new SnapshotQueryDirectory(
                  shardContainer, blobStoreSnapshot, profilingRecorder, indexName, shardId);
          DirectoryReader reader = DirectoryReader.open(directory)) {
        long openNanos = System.nanoTime() - openStartNanos;
        if (profilingRecorder != null) {
          profilingRecorder.recordShardOpen(indexName, shardId, openNanos);
          profilingRecorder.startShard(indexName, shardId, shardCount, "searching");
        }
        IndexSearcher searcher = new IndexSearcher(reader);
        long shardStartNanos = System.nanoTime();
        long shardExported =
            exportShard(
                searcher,
                luceneQuery,
                sort,
                sourceFields,
                batchSize,
                out,
                null,
                profilingRecorder,
                indexName,
                shardId);
        long shardSearchNanos = System.nanoTime() - shardStartNanos;
        long shardTookMs = shardSearchNanos / 1_000_000L;
        totalExported += shardExported;

        long elapsed = (System.currentTimeMillis() - startTimeMillis) / 1000;
        if (progressReporter != null) progressReporter.clearLine();
        terminal.errorPrintln(
            "  Shard "
                + shardId
                + ": exported "
                + shardExported
                + " docs (index total: "
                + totalExported
                + ", open: "
                + (openNanos / 1_000_000L)
                + "ms, shard: "
                + shardTookMs
                + "ms, elapsed: "
                + elapsed
                + "s)");
        if (profilingRecorder != null) {
          profilingRecorder.deactivateShard(indexName, shardId);
        }
      }
    }

    return totalExported;
  }

  private static long exportShardsParallel(
      Terminal terminal,
      SnapshotMetadataLoader metadataLoader,
      SnapshotMetadataLoader.ResolvedIndex resolved,
      Query luceneQuery,
      Sort sort,
      List<String> sourceFields,
      int batchSize,
      OutputStream out,
      long startTimeMillis,
      ProfilingRecorder profilingRecorder,
      ProgressReporter progressReporter,
      int parallelShards)
      throws IOException {
    String indexName = resolved.indexId().getName();
    int shardCount = resolved.shardSnapshots().size();
    Object writeLock = new Object();
    ExecutorService executor = Executors.newFixedThreadPool(parallelShards);
    List<Future<Long>> futures = new ArrayList<>();

    for (SnapshotMetadataLoader.ShardSnapshot shardSnapshot : resolved.shardSnapshots()) {
      futures.add(
          executor.submit(
              () -> {
                int shardId = shardSnapshot.shardId();
                BlobContainer shardContainer =
                    metadataLoader.shardContainer(resolved.indexId(), shardId);
                BlobStoreIndexShardSnapshot blobStoreSnapshot = shardSnapshot.snapshot();
                if (profilingRecorder != null) {
                  profilingRecorder.activateShard(indexName, shardId, "opening");
                  profilingRecorder.startShard(indexName, shardId, shardCount, "opening");
                }

                long openStartNanos = System.nanoTime();
                try (Directory directory =
                        new SnapshotQueryDirectory(
                            shardContainer,
                            blobStoreSnapshot,
                            profilingRecorder,
                            indexName,
                            shardId);
                    DirectoryReader reader = DirectoryReader.open(directory)) {
                  long openNanos = System.nanoTime() - openStartNanos;
                  if (profilingRecorder != null) {
                    profilingRecorder.recordShardOpen(indexName, shardId, openNanos);
                    profilingRecorder.updateShardStage(indexName, shardId, "searching");
                  }
                  IndexSearcher searcher = new IndexSearcher(reader);
                  long shardStartNanos = System.nanoTime();
                  long shardExported =
                      exportShard(
                          searcher,
                          luceneQuery,
                          sort,
                          sourceFields,
                          batchSize,
                          out,
                          writeLock,
                          profilingRecorder,
                          indexName,
                          shardId);
                  long shardSearchNanos = System.nanoTime() - shardStartNanos;
                  long shardTookMs = shardSearchNanos / 1_000_000L;

                  long elapsed = (System.currentTimeMillis() - startTimeMillis) / 1000;
                  if (progressReporter != null) progressReporter.clearLine();
                  terminal.errorPrintln(
                      "  Shard "
                          + shardId
                          + ": exported "
                          + shardExported
                          + " docs (open: "
                          + (openNanos / 1_000_000L)
                          + "ms, shard: "
                          + shardTookMs
                          + "ms, elapsed: "
                          + elapsed
                          + "s)");
                  if (profilingRecorder != null) {
                    profilingRecorder.deactivateShard(indexName, shardId);
                  }
                  return shardExported;
                }
              }));
    }

    executor.shutdown();
    long totalExported = 0;
    IOException failure = null;
    for (Future<Long> future : futures) {
      try {
        totalExported += future.get();
      } catch (ExecutionException e) {
        executor.shutdownNow();
        Throwable cause = e.getCause();
        if (cause instanceof IOException ioe) {
          failure = ioe;
        } else {
          failure = new IOException("Shard export failed", cause);
        }
        break;
      } catch (InterruptedException e) {
        executor.shutdownNow();
        Thread.currentThread().interrupt();
        failure = new IOException("Shard export interrupted", e);
        break;
      }
    }
    if (failure != null) {
      throw failure;
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

  @SuppressForbidden(reason = "file output for cli")
  static AtomicFileOutput openAtomicOutput(String finalPath, String compression)
      throws IOException {
    String tempPath = finalPath + ".tmp";
    OutputStream out = openOutput(tempPath, compression);
    return new AtomicFileOutput(out, PathUtils.get(tempPath), PathUtils.get(finalPath));
  }

  static final class AtomicFileOutput implements Closeable {
    private final OutputStream out;
    private final Path tempPath;
    private final Path finalPath;
    private boolean committed = false;

    AtomicFileOutput(OutputStream out, Path tempPath, Path finalPath) {
      this.out = out;
      this.tempPath = tempPath;
      this.finalPath = finalPath;
    }

    OutputStream stream() {
      return out;
    }

    @SuppressForbidden(reason = "atomic rename for cli")
    void commit() throws IOException {
      out.flush();
      out.close();
      try {
        Files.move(
            tempPath,
            finalPath,
            StandardCopyOption.REPLACE_EXISTING,
            StandardCopyOption.ATOMIC_MOVE);
      } catch (AtomicMoveNotSupportedException e) {
        Files.move(tempPath, finalPath, StandardCopyOption.REPLACE_EXISTING);
      }
      committed = true;
    }

    @Override
    @SuppressForbidden(reason = "cleanup temp file for cli")
    public void close() throws IOException {
      if (!committed) {
        try {
          out.close();
        } catch (IOException ignored) {
        }
        Files.deleteIfExists(tempPath);
      }
    }
  }

  static long exportShard(
      IndexSearcher searcher,
      Query query,
      Sort sort,
      List<String> sourceFields,
      int batchSize,
      OutputStream out,
      Object writeLock,
      ProfilingRecorder profilingRecorder,
      String indexName,
      int shardId)
      throws IOException {
    long exported = 0;
    ScoreDoc lastDoc = null;
    Set<String> fieldSet = sourceFields != null ? new HashSet<>(sourceFields) : null;

    while (true) {
      TopDocs topDocs;
      long batchStartNanos = System.nanoTime();
      if (lastDoc == null) {
        topDocs = searcher.search(query, batchSize, sort);
      } else {
        topDocs = searcher.searchAfter(lastDoc, query, batchSize, sort);
      }

      if (topDocs.scoreDocs.length == 0) {
        break;
      }

      if (lastDoc == null && profilingRecorder != null && topDocs.totalHits != null) {
        profilingRecorder.setShardTotalHits(indexName, shardId, topDocs.totalHits.value());
      }

      // Fetch stored fields (S3 reads happen here, outside the lock)
      StoredFields storedFields = searcher.storedFields();
      List<byte[]> batchBytes = new ArrayList<>(topDocs.scoreDocs.length);
      for (ScoreDoc scoreDoc : topDocs.scoreDocs) {
        var doc = storedFields.document(scoreDoc.doc);
        var sourceField = doc.getBinaryValue("_source");
        if (sourceField != null) {
          batchBytes.add(
              filterSource(sourceField.bytes, sourceField.offset, sourceField.length, fieldSet));
        }
      }

      // Write batch to output (synchronized when concurrent)
      if (writeLock != null) {
        synchronized (writeLock) {
          writeBatch(out, batchBytes);
        }
      } else {
        writeBatch(out, batchBytes);
      }

      exported += batchBytes.size();
      if (profilingRecorder != null && !batchBytes.isEmpty()) {
        profilingRecorder.recordShardSearch(
            indexName, shardId, batchBytes.size(), System.nanoTime() - batchStartNanos);
      }

      lastDoc = topDocs.scoreDocs[topDocs.scoreDocs.length - 1];
    }

    return exported;
  }

  private static void writeBatch(OutputStream out, List<byte[]> batchBytes) throws IOException {
    for (byte[] sourceBytes : batchBytes) {
      out.write(sourceBytes);
      out.write('\n');
    }
  }

  private static byte[] filterSource(byte[] bytes, int offset, int length, Set<String> fields)
      throws IOException {
    if (fields == null || fields.isEmpty()) {
      return compactJson(bytes, offset, length);
    }

    try (XContentParser parser =
        XContentType.JSON
            .xContent()
            .createParser(XContentParserConfiguration.EMPTY, bytes, offset, length)) {
      parser.nextToken();
      Map<String, Object> filtered = filterObject(parser, fields, "");

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

  /**
   * Recursively filter an object's fields based on dotted field paths. E.g.,
   * fields={"host.name","message"} with prefix="" will include "message" directly and recurse into
   * "host" looking for "name".
   */
  private static Map<String, Object> filterObject(
      XContentParser parser, Set<String> fields, String prefix) throws IOException {
    Map<String, Object> filtered = new HashMap<>();
    while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
      String fieldName = parser.currentName();
      parser.nextToken();
      String fullPath = prefix.isEmpty() ? fieldName : prefix + "." + fieldName;

      if (fields.contains(fullPath)) {
        // Exact match — include the whole value
        filtered.put(fieldName, parseValue(parser));
      } else if (parser.currentToken() == XContentParser.Token.START_OBJECT
          && hasChildFields(fields, fullPath)) {
        // This is a parent of a requested dotted path — recurse
        Map<String, Object> nested = filterObject(parser, fields, fullPath);
        if (!nested.isEmpty()) {
          filtered.put(fieldName, nested);
        }
      } else {
        parser.skipChildren();
      }
    }
    return filtered;
  }

  /** Check if any field in the set starts with the given prefix followed by a dot. */
  private static boolean hasChildFields(Set<String> fields, String prefix) {
    String prefixDot = prefix + ".";
    for (String field : fields) {
      if (field.startsWith(prefixDot)) {
        return true;
      }
    }
    return false;
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
    return s.replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t");
  }
}
