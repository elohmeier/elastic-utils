package org.elasticsearch.snapshotquery;

import org.elasticsearch.cluster.metadata.IndexMetadata;
import org.elasticsearch.cluster.metadata.ProjectId;
import org.elasticsearch.common.Numbers;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.common.blobstore.BlobPath;
import org.elasticsearch.common.blobstore.OperationPurpose;
import org.elasticsearch.common.bytes.BytesReference;
import org.elasticsearch.common.xcontent.LoggingDeprecationHandler;
import org.elasticsearch.common.io.Streams;
import org.elasticsearch.index.snapshots.blobstore.BlobStoreIndexShardSnapshot;
import org.elasticsearch.repositories.IndexId;
import org.elasticsearch.repositories.RepositoryData;
import org.elasticsearch.snapshots.SnapshotId;
import org.elasticsearch.snapshots.SnapshotInfo;
import org.elasticsearch.repositories.ProjectRepo;
import org.elasticsearch.repositories.blobstore.BlobStoreRepository;
import org.elasticsearch.xcontent.NamedXContentRegistry;
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentType;

import java.io.IOException;
import java.io.InputStream;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Loads snapshot metadata from S3 without requiring a running Elasticsearch cluster.
 */
public class SnapshotMetadataLoader {

    private static final ProjectRepo DUMMY_REPO = new ProjectRepo(ProjectId.DEFAULT, "_");
    private static final Pattern INDEX_DATE_PATTERN = Pattern.compile("(\\d{4}\\.\\d{2}\\.\\d{2})");

    private final BlobContainer rootContainer;
    private final S3ClientFactory.S3Access s3Access;

    public SnapshotMetadataLoader(BlobContainer rootContainer, S3ClientFactory.S3Access s3Access) {
        this.rootContainer = rootContainer;
        this.s3Access = s3Access;
    }

    /**
     * Resolves snapshot name and index name to shard-level snapshot metadata.
     */
    public ResolvedIndex resolve(String snapshotName, String indexName) throws IOException {
        RepositoryData repositoryData = loadRepositoryData();

        // Find snapshot by name
        SnapshotId snapshotId = null;
        for (SnapshotId id : repositoryData.getSnapshotIds()) {
            if (id.getName().equals(snapshotName)) {
                snapshotId = id;
                break;
            }
        }
        if (snapshotId == null) {
            throw new IllegalArgumentException("Snapshot [" + snapshotName + "] not found. Available: "
                + repositoryData.getSnapshotIds().stream().map(SnapshotId::getName).toList());
        }

        // Find index
        IndexId indexId = repositoryData.getIndices().get(indexName);
        if (indexId == null) {
            throw new IllegalArgumentException("Index [" + indexName + "] not found in snapshot. Available: "
                + repositoryData.getIndices().keySet());
        }

        // Load index metadata to get shard count
        String metaBlobId = repositoryData.indexMetaDataGenerations().indexMetaBlobId(snapshotId, indexId);
        BlobContainer indexContainer = indexContainer(indexId);
        IndexMetadata indexMetadata = BlobStoreRepository.INDEX_METADATA_FORMAT.read(
            DUMMY_REPO, indexContainer, metaBlobId, NamedXContentRegistry.EMPTY
        );

        int shardCount = indexMetadata.getNumberOfShards();

        // Load shard snapshots
        List<ShardSnapshot> shardSnapshots = new ArrayList<>(shardCount);
        for (int shard = 0; shard < shardCount; shard++) {
            BlobContainer shardCont = shardContainer(indexId, shard);
            BlobStoreIndexShardSnapshot shardSnapshot = BlobStoreRepository.INDEX_SHARD_SNAPSHOT_FORMAT.read(
                DUMMY_REPO, shardCont, snapshotId.getUUID(), NamedXContentRegistry.EMPTY
            );
            shardSnapshots.add(new ShardSnapshot(shard, shardSnapshot));
        }

        return new ResolvedIndex(snapshotId, indexId, indexMetadata, shardSnapshots);
    }

    private RepositoryData loadRepositoryData() throws IOException {
        // Read index.latest to find current generation
        long generation = readIndexLatest();

        // Read index-N blob (JSON format)
        String indexBlobName = BlobStoreRepository.INDEX_FILE_PREFIX + generation;
        try (
            InputStream blob = rootContainer.readBlob(OperationPurpose.SNAPSHOT_METADATA, indexBlobName);
            XContentParser parser = XContentType.JSON.xContent()
                .createParser(NamedXContentRegistry.EMPTY, LoggingDeprecationHandler.INSTANCE, blob)
        ) {
            return RepositoryData.snapshotsFromXContent(parser, generation, true);
        }
    }

    private long readIndexLatest() throws IOException {
        BytesReference content = Streams.readFully(
            Streams.limitStream(
                rootContainer.readBlob(OperationPurpose.SNAPSHOT_METADATA, BlobStoreRepository.INDEX_LATEST_BLOB),
                Long.BYTES + 1
            )
        );
        if (content.length() != Long.BYTES) {
            throw new IOException("Invalid index.latest blob: expected 8 bytes but got " + content.length());
        }
        return Numbers.bytesToLong(content.toBytesRef());
    }

    public BlobContainer indexContainer(IndexId indexId) {
        return s3Access.containerFor(appendToRoot("indices", indexId.getId()));
    }

    public BlobContainer shardContainer(IndexId indexId, int shardId) {
        return s3Access.containerFor(appendToRoot("indices", indexId.getId(), Integer.toString(shardId)));
    }

    private BlobPath appendToRoot(String... parts) {
        BlobPath path = rootContainer.path();
        for (String part : parts) {
            path = path.add(part);
        }
        return path;
    }

    /**
     * Resolves an index name that may be an alias or wildcard pattern to all matching concrete indices in the snapshot.
     */
    public List<ResolvedIndex> resolveIndices(String snapshotName, String indexNameOrAlias) throws IOException {
        RepositoryData repositoryData = loadRepositoryData();

        SnapshotId snapshotId = null;
        for (SnapshotId id : repositoryData.getSnapshotIds()) {
            if (id.getName().equals(snapshotName)) {
                snapshotId = id;
                break;
            }
        }
        if (snapshotId == null) {
            throw new IllegalArgumentException("Snapshot [" + snapshotName + "] not found. Available: "
                + repositoryData.getSnapshotIds().stream().map(SnapshotId::getName).toList());
        }

        // Get indices that are part of this snapshot
        var snapshotIndices = repositoryData.getIndices();
        List<ResolvedIndex> results = new ArrayList<>();

        // First try exact match on index name
        IndexId exactMatch = snapshotIndices.get(indexNameOrAlias);
        if (exactMatch != null) {
            results.add(resolveWithId(repositoryData, snapshotId, exactMatch));
            return results;
        }

        // Check if it's an alias or wildcard pattern
        boolean isWildcard = indexNameOrAlias.contains("*");

        int skipped = 0;
        for (var entry : snapshotIndices.entrySet()) {
            String concreteIndexName = entry.getKey();
            IndexId indexId = entry.getValue();

            // Skip indices not in this snapshot
            if (!repositoryData.getSnapshots(indexId).contains(snapshotId)) {
                continue;
            }

            boolean matches = false;
            if (isWildcard) {
                matches = wildcardMatch(concreteIndexName, indexNameOrAlias);
            }

            if (!matches) {
                // Check aliases: load index metadata and check getAliases()
                try {
                    String metaBlobId = repositoryData.indexMetaDataGenerations().indexMetaBlobId(snapshotId, indexId);
                    BlobContainer idxContainer = indexContainer(indexId);
                    IndexMetadata indexMetadata = BlobStoreRepository.INDEX_METADATA_FORMAT.read(
                        null, idxContainer, metaBlobId, NamedXContentRegistry.EMPTY
                    );

                    if (indexMetadata.getAliases().containsKey(indexNameOrAlias)) {
                        matches = true;
                    }
                } catch (Exception e) {
                    skipped++;
                    continue;
                }
            }

            if (matches) {
                try {
                    results.add(resolveWithId(repositoryData, snapshotId, indexId));
                } catch (Exception e) {
                    System.err.println("WARN: Skipping index [" + concreteIndexName + "]: " + e.getMessage());
                }
            }
        }
        if (skipped > 0) {
            System.err.println("WARN: Skipped " + skipped + " indices with missing metadata");
        }

        if (results.isEmpty()) {
            throw new IllegalArgumentException("No index or alias [" + indexNameOrAlias + "] found in snapshot [" + snapshotName
                + "]. Available indices: " + snapshotIndices.keySet());
        }

        return results;
    }

    private ResolvedIndex resolveWithId(RepositoryData repositoryData, SnapshotId snapshotId, IndexId indexId) throws IOException {
        String metaBlobId = repositoryData.indexMetaDataGenerations().indexMetaBlobId(snapshotId, indexId);
        BlobContainer idxContainer = indexContainer(indexId);
        IndexMetadata indexMetadata = BlobStoreRepository.INDEX_METADATA_FORMAT.read(
            null, idxContainer, metaBlobId, NamedXContentRegistry.EMPTY
        );

        int shardCount = indexMetadata.getNumberOfShards();
        List<ShardSnapshot> shardSnapshots = new ArrayList<>(shardCount);
        for (int shard = 0; shard < shardCount; shard++) {
            BlobContainer shardCont = shardContainer(indexId, shard);
            BlobStoreIndexShardSnapshot shardSnapshot = BlobStoreRepository.INDEX_SHARD_SNAPSHOT_FORMAT.read(
                DUMMY_REPO, shardCont, snapshotId.getUUID(), NamedXContentRegistry.EMPTY
            );
            shardSnapshots.add(new ShardSnapshot(shard, shardSnapshot));
        }

        return new ResolvedIndex(snapshotId, indexId, indexMetadata, shardSnapshots);
    }

    private static boolean wildcardMatch(String text, String pattern) {
        String regex = "^" + pattern.replace(".", "\\.").replace("*", ".*") + "$";
        return text.matches(regex);
    }

    /**
     * Lists all snapshots in the repository with their metadata.
     */
    public List<SnapshotSummary> listSnapshots() throws IOException {
        RepositoryData repositoryData = loadRepositoryData();
        List<SnapshotSummary> summaries = new ArrayList<>();

        for (SnapshotId snapshotId : repositoryData.getSnapshotIds()) {
            SnapshotInfo info;
            try {
                info = BlobStoreRepository.SNAPSHOT_FORMAT.read(
                    DUMMY_REPO, rootContainer, snapshotId.getUUID(), NamedXContentRegistry.EMPTY
                );
            } catch (Exception e) {
                // Skip snapshots whose info can't be loaded
                continue;
            }

            List<String> indices = info.indices();
            summaries.add(new SnapshotSummary(
                snapshotId,
                info.state().toString(),
                info.startTime(),
                info.endTime(),
                indices
            ));
        }

        // Sort by start time
        summaries.sort((a, b) -> Long.compare(a.startTimeMillis(), b.startTimeMillis()));
        return summaries;
    }

    /**
     * Find snapshots that contain the given index name or alias.
     * Returns snapshot names sorted by start time (newest first).
     */
    public List<String> findSnapshotsForIndex(String indexNameOrAlias) throws IOException {
        return listSnapshotsForIndex(indexNameOrAlias).stream().map(s -> s.snapshotId().getName()).toList();
    }

    public List<SnapshotSummary> listSnapshotsForIndex(String indexNameOrAlias) throws IOException {
        RepositoryData repositoryData = loadRepositoryData();
        var allIndices = repositoryData.getIndices();
        List<SnapshotSummary> matching = new ArrayList<>();

        // Check if it's a direct index name
        IndexId directMatch = allIndices.get(indexNameOrAlias);

        for (SnapshotId snapshotId : repositoryData.getSnapshotIds()) {
            boolean found = false;

            if (directMatch != null) {
                // Check if this snapshot contains the index
                var snapshotIndexIds = repositoryData.getIndices();
                // RepositoryData.getIndices() gives all indices across all snapshots,
                // so check via indexSnapshots
                List<SnapshotId> snapshots = repositoryData.getSnapshots(directMatch);
                found = snapshots.contains(snapshotId);
            }

            if (!found) {
                // Check aliases: need to look at each index's metadata in this snapshot
                for (var entry : allIndices.entrySet()) {
                    IndexId indexId = entry.getValue();
                    List<SnapshotId> snapshots = repositoryData.getSnapshots(indexId);
                    if (!snapshots.contains(snapshotId)) continue;

                    try {
                        String metaBlobId = repositoryData.indexMetaDataGenerations().indexMetaBlobId(snapshotId, indexId);
                        BlobContainer idxContainer = indexContainer(indexId);
                        IndexMetadata indexMetadata = BlobStoreRepository.INDEX_METADATA_FORMAT.read(
                            null, idxContainer, metaBlobId, NamedXContentRegistry.EMPTY
                        );
                        if (indexMetadata.getAliases().containsKey(indexNameOrAlias)) {
                            found = true;
                            break;
                        }
                    } catch (Exception e) {
                        // skip unreadable metadata
                    }
                }
            }

            if (found) {
                try {
                    SnapshotInfo info = BlobStoreRepository.SNAPSHOT_FORMAT.read(
                        DUMMY_REPO, rootContainer, snapshotId.getUUID(), NamedXContentRegistry.EMPTY
                    );
                    matching.add(new SnapshotSummary(
                        snapshotId, info.state().toString(), info.startTime(), info.endTime(), info.indices()
                    ));
                } catch (Exception e) {
                    // include without time info
                    matching.add(new SnapshotSummary(snapshotId, "UNKNOWN", 0, 0, List.of()));
                }
            }
        }

        // Sort newest first
        matching.sort((a, b) -> Long.compare(b.startTimeMillis(), a.startTimeMillis()));
        return matching;
    }

    public List<ExportTarget> findExportTargets(
        String indexPattern,
        LocalDate fromIndexDate,
        LocalDate toIndexDate,
        boolean latestPerIndex
    ) throws IOException {
        List<SnapshotSummary> snapshots = listSnapshots();
        snapshots.sort(Comparator.comparingLong(SnapshotSummary::startTimeMillis).reversed());

        List<ExportTarget> discovered = new ArrayList<>();
        for (SnapshotSummary summary : snapshots) {
            for (String indexName : summary.indices()) {
                if (!wildcardMatch(indexName, indexPattern)) {
                    continue;
                }

                LocalDate indexDate = extractIndexDate(indexName);
                if (indexDate == null) {
                    continue;
                }
                if (indexDate.isBefore(fromIndexDate) || indexDate.isAfter(toIndexDate)) {
                    continue;
                }

                discovered.add(new ExportTarget(
                    summary.snapshotId().getName(),
                    indexName,
                    summary.state(),
                    summary.startTimeMillis(),
                    indexDate
                ));
            }
        }

        if (!latestPerIndex) {
            discovered.sort(Comparator.comparingLong(ExportTarget::snapshotStartTimeMillis));
            return discovered;
        }

        Map<String, ExportTarget> latest = new LinkedHashMap<>();
        for (ExportTarget target : discovered) {
            latest.putIfAbsent(target.indexName(), target);
        }
        List<ExportTarget> result = new ArrayList<>(latest.values());
        result.sort(Comparator
            .comparing(ExportTarget::indexDate)
            .thenComparingLong(ExportTarget::snapshotStartTimeMillis));
        return result;
    }

    private static LocalDate extractIndexDate(String indexName) {
        Matcher matcher = INDEX_DATE_PATTERN.matcher(indexName);
        if (!matcher.find()) {
            return null;
        }
        return LocalDate.parse(matcher.group(1).replace('.', '-'));
    }

    public record ShardSnapshot(int shardId, BlobStoreIndexShardSnapshot snapshot) {}

    public record ResolvedIndex(SnapshotId snapshotId, IndexId indexId, IndexMetadata indexMetadata, List<ShardSnapshot> shardSnapshots) {}

    public record SnapshotSummary(SnapshotId snapshotId, String state, long startTimeMillis, long endTimeMillis, List<String> indices) {}

    public record ExportTarget(
        String snapshotName,
        String indexName,
        String state,
        long snapshotStartTimeMillis,
        LocalDate indexDate
    ) {}
}
