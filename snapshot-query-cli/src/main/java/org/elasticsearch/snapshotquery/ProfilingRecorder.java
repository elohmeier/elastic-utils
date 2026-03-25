package org.elasticsearch.snapshotquery;

import org.elasticsearch.xcontent.XContentBuilder;
import org.elasticsearch.xcontent.XContentType;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

final class ProfilingRecorder {

    private final long startedAtNanos = System.nanoTime();
    private volatile boolean partial;
    private volatile String exitReason = "running";
    private volatile String currentSnapshotName;
    private volatile String currentIndexName;
    private volatile int currentShardId = -1;
    private volatile String currentStage = "starting";
    private volatile int completedTargets;
    private volatile int totalTargets;

    private final Map<String, AtomicLong> phaseNanos = new ConcurrentHashMap<>();
    private final AtomicLong s3HeadCalls = new AtomicLong();
    private final AtomicLong s3HeadNanos = new AtomicLong();
    private final AtomicLong s3ListCalls = new AtomicLong();
    private final AtomicLong s3ListNanos = new AtomicLong();
    private final AtomicLong s3ReadFullCalls = new AtomicLong();
    private final AtomicLong s3ReadFullNanos = new AtomicLong();
    private final AtomicLong s3ReadRangeCalls = new AtomicLong();
    private final AtomicLong s3ReadRangeNanos = new AtomicLong();
    private final AtomicLong s3BytesRequested = new AtomicLong();
    private final AtomicLong s3BytesRead = new AtomicLong();
    private final AtomicLong liveDocsExported = new AtomicLong();

    private final Map<String, IndexStats> indices = new ConcurrentHashMap<>();
    private final Map<String, LuceneFileStats> luceneFiles = new ConcurrentHashMap<>();

    void addPhaseNanos(String phase, long nanos) {
        phaseNanos.computeIfAbsent(phase, ignored -> new AtomicLong()).addAndGet(nanos);
    }

    void setTargetTotals(int completedTargets, int totalTargets) {
        this.completedTargets = completedTargets;
        this.totalTargets = totalTargets;
    }

    void startTarget(String snapshotName, String indexName, int completedTargets, int totalTargets) {
        currentSnapshotName = snapshotName;
        currentIndexName = indexName;
        currentShardId = -1;
        currentStage = "resolving";
        setTargetTotals(completedTargets, totalTargets);
    }

    void setCurrentStage(String stage) {
        currentStage = stage;
    }

    void startShard(String indexName, int shardId, String stage) {
        currentIndexName = indexName;
        currentShardId = shardId;
        currentStage = stage;
    }

    void finishTarget(int completedTargets, int totalTargets) {
        this.completedTargets = completedTargets;
        this.totalTargets = totalTargets;
        currentShardId = -1;
        currentStage = "idle";
    }

    void markCompleted() {
        partial = false;
        exitReason = "completed";
        currentStage = "completed";
    }

    void markInterrupted() {
        partial = true;
        exitReason = "interrupted";
        currentStage = "interrupted";
    }

    void recordS3Head(long nanos) {
        s3HeadCalls.incrementAndGet();
        s3HeadNanos.addAndGet(nanos);
    }

    void recordS3List(long nanos) {
        s3ListCalls.incrementAndGet();
        s3ListNanos.addAndGet(nanos);
    }

    void recordS3Read(boolean ranged, long requestedBytes, long readBytes, long nanos) {
        if (ranged) {
            s3ReadRangeCalls.incrementAndGet();
            s3ReadRangeNanos.addAndGet(nanos);
        } else {
            s3ReadFullCalls.incrementAndGet();
            s3ReadFullNanos.addAndGet(nanos);
        }
        s3BytesRequested.addAndGet(requestedBytes);
        s3BytesRead.addAndGet(readBytes);
    }

    void recordIndexResolve(String indexName, String snapshotName, long nanos) {
        IndexStats indexStats = indices.computeIfAbsent(indexName, ignored -> new IndexStats(indexName, snapshotName));
        indexStats.snapshotName = snapshotName;
        indexStats.resolveNanos.addAndGet(nanos);
    }

    void recordIndexExport(String indexName, String snapshotName, String outputPath, long docsExported, long nanos) {
        IndexStats indexStats = indices.computeIfAbsent(indexName, ignored -> new IndexStats(indexName, snapshotName));
        indexStats.snapshotName = snapshotName;
        indexStats.outputPath = outputPath;
        indexStats.docsExported.addAndGet(docsExported);
        indexStats.exportNanos.addAndGet(nanos);
    }

    void recordShardOpen(String indexName, int shardId, long nanos) {
        ShardStats shardStats = shardStats(indexName, shardId);
        shardStats.openNanos.addAndGet(nanos);
    }

    void recordShardSearch(String indexName, int shardId, long docsExported, long nanos) {
        ShardStats shardStats = shardStats(indexName, shardId);
        shardStats.docsExported.addAndGet(docsExported);
        shardStats.searchNanos.addAndGet(nanos);
        liveDocsExported.addAndGet(docsExported);
    }

    void recordLuceneFileRead(String indexName, int shardId, String fileName, long bytesRead, long nanos) {
        String key = indexName + "#" + shardId + ":" + fileName;
        LuceneFileStats fileStats = luceneFiles.computeIfAbsent(key, ignored -> new LuceneFileStats(indexName, shardId, fileName));
        fileStats.readCalls.incrementAndGet();
        fileStats.bytesRead.addAndGet(bytesRead);
        fileStats.readNanos.addAndGet(nanos);
    }

    String renderJson() throws IOException {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (XContentBuilder builder = new XContentBuilder(XContentType.JSON.xContent(), baos)) {
            builder.prettyPrint();
            builder.startObject();

            builder.startObject("summary");
            builder.field("total_ms", nanosToMillis(System.nanoTime() - startedAtNanos));
            builder.field("partial", partial);
            builder.field("exit_reason", exitReason);
            builder.endObject();

            builder.startObject("phases");
            for (Map.Entry<String, AtomicLong> entry : phaseNanos.entrySet().stream().sorted(Map.Entry.comparingByKey()).toList()) {
                builder.field(entry.getKey() + "_ms", nanosToMillis(entry.getValue().get()));
            }
            builder.endObject();

            builder.startObject("s3");
            builder.field("head_calls", s3HeadCalls.get());
            builder.field("head_ms", nanosToMillis(s3HeadNanos.get()));
            builder.field("list_calls", s3ListCalls.get());
            builder.field("list_ms", nanosToMillis(s3ListNanos.get()));
            builder.field("read_full_calls", s3ReadFullCalls.get());
            builder.field("read_full_ms", nanosToMillis(s3ReadFullNanos.get()));
            builder.field("read_range_calls", s3ReadRangeCalls.get());
            builder.field("read_range_ms", nanosToMillis(s3ReadRangeNanos.get()));
            builder.field("bytes_requested", s3BytesRequested.get());
            builder.field("bytes_read", s3BytesRead.get());
            builder.endObject();

            builder.startArray("indices");
            for (IndexStats indexStats : indices.values().stream().sorted(Comparator.comparing(stats -> stats.indexName)).toList()) {
                builder.startObject();
                builder.field("index", indexStats.indexName);
                builder.field("snapshot", indexStats.snapshotName);
                if (indexStats.outputPath != null) {
                    builder.field("output_path", indexStats.outputPath);
                }
                builder.field("resolve_ms", nanosToMillis(indexStats.resolveNanos.get()));
                builder.field("export_ms", nanosToMillis(indexStats.exportNanos.get()));
                builder.field("docs_exported", indexStats.docsExported.get());

                builder.startArray("shards");
                List<ShardStats> shardStatsList = new ArrayList<>(indexStats.shards.values());
                shardStatsList.sort(Comparator.comparingInt(stats -> stats.shardId));
                for (ShardStats shardStats : shardStatsList) {
                    builder.startObject();
                    builder.field("shard", shardStats.shardId);
                    builder.field("open_ms", nanosToMillis(shardStats.openNanos.get()));
                    builder.field("search_ms", nanosToMillis(shardStats.searchNanos.get()));
                    builder.field("docs_exported", shardStats.docsExported.get());
                    builder.endObject();
                }
                builder.endArray();
                builder.endObject();
            }
            builder.endArray();

            builder.startArray("lucene_files");
            for (LuceneFileStats fileStats : luceneFiles.values().stream()
                .sorted(Comparator.comparing((LuceneFileStats stats) -> stats.indexName).thenComparingInt(stats -> stats.shardId).thenComparing(stats -> stats.fileName))
                .toList()) {
                builder.startObject();
                builder.field("index", fileStats.indexName);
                builder.field("shard", fileStats.shardId);
                builder.field("file", fileStats.fileName);
                builder.field("read_calls", fileStats.readCalls.get());
                builder.field("bytes_read", fileStats.bytesRead.get());
                builder.field("read_ms", nanosToMillis(fileStats.readNanos.get()));
                builder.endObject();
            }
            builder.endArray();

            builder.endObject();
        }
        return baos.toString();
    }

    long totalMillis() {
        return nanosToMillis(System.nanoTime() - startedAtNanos);
    }

    long s3BytesRead() {
        return s3BytesRead.get();
    }

    long s3ReadRangeCalls() {
        return s3ReadRangeCalls.get();
    }

    long s3ReadFullCalls() {
        return s3ReadFullCalls.get();
    }

    long totalDocsExported() {
        return liveDocsExported.get();
    }

    String currentSnapshotName() {
        return currentSnapshotName;
    }

    String currentIndexName() {
        return currentIndexName;
    }

    int currentShardId() {
        return currentShardId;
    }

    String currentStage() {
        return currentStage;
    }

    int completedTargets() {
        return completedTargets;
    }

    int totalTargets() {
        return totalTargets;
    }

    private ShardStats shardStats(String indexName, int shardId) {
        IndexStats indexStats = indices.computeIfAbsent(indexName, ignored -> new IndexStats(indexName, null));
        return indexStats.shards.computeIfAbsent(shardId, ignored -> new ShardStats(shardId));
    }

    private static long nanosToMillis(long nanos) {
        return nanos / 1_000_000L;
    }

    private static final class IndexStats {
        private final String indexName;
        private volatile String snapshotName;
        private volatile String outputPath;
        private final AtomicLong resolveNanos = new AtomicLong();
        private final AtomicLong exportNanos = new AtomicLong();
        private final AtomicLong docsExported = new AtomicLong();
        private final Map<Integer, ShardStats> shards = new ConcurrentHashMap<>();

        private IndexStats(String indexName, String snapshotName) {
            this.indexName = indexName;
            this.snapshotName = snapshotName;
        }
    }

    private static final class ShardStats {
        private final int shardId;
        private final AtomicLong openNanos = new AtomicLong();
        private final AtomicLong searchNanos = new AtomicLong();
        private final AtomicLong docsExported = new AtomicLong();

        private ShardStats(int shardId) {
            this.shardId = shardId;
        }
    }

    private static final class LuceneFileStats {
        private final String indexName;
        private final int shardId;
        private final String fileName;
        private final AtomicLong readCalls = new AtomicLong();
        private final AtomicLong bytesRead = new AtomicLong();
        private final AtomicLong readNanos = new AtomicLong();

        private LuceneFileStats(String indexName, int shardId, String fileName) {
            this.indexName = indexName;
            this.shardId = shardId;
            this.fileName = fileName;
        }
    }
}
