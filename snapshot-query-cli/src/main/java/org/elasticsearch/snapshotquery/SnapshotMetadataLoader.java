package org.elasticsearch.snapshotquery;

import org.elasticsearch.cluster.metadata.IndexMetadata;
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
import org.elasticsearch.repositories.blobstore.BlobStoreRepository;
import org.elasticsearch.xcontent.NamedXContentRegistry;
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentType;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * Loads snapshot metadata from S3 without requiring a running Elasticsearch cluster.
 */
public class SnapshotMetadataLoader {

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
            null, indexContainer, metaBlobId, NamedXContentRegistry.EMPTY
        );

        int shardCount = indexMetadata.getNumberOfShards();

        // Load shard snapshots
        List<ShardSnapshot> shardSnapshots = new ArrayList<>(shardCount);
        for (int shard = 0; shard < shardCount; shard++) {
            BlobContainer shardCont = shardContainer(indexId, shard);
            BlobStoreIndexShardSnapshot shardSnapshot = BlobStoreRepository.INDEX_SHARD_SNAPSHOT_FORMAT.read(
                null, shardCont, snapshotId.getUUID(), NamedXContentRegistry.EMPTY
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

        for (var entry : snapshotIndices.entrySet()) {
            String concreteIndexName = entry.getKey();
            IndexId indexId = entry.getValue();

            boolean matches = false;
            if (isWildcard) {
                matches = wildcardMatch(concreteIndexName, indexNameOrAlias);
            }

            if (!matches) {
                // Check aliases: load index metadata and check getAliases()
                String metaBlobId = repositoryData.indexMetaDataGenerations().indexMetaBlobId(snapshotId, indexId);
                BlobContainer idxContainer = indexContainer(indexId);
                IndexMetadata indexMetadata = BlobStoreRepository.INDEX_METADATA_FORMAT.read(
                    null, idxContainer, metaBlobId, NamedXContentRegistry.EMPTY
                );

                if (indexMetadata.getAliases().containsKey(indexNameOrAlias)) {
                    matches = true;
                }
            }

            if (matches) {
                results.add(resolveWithId(repositoryData, snapshotId, indexId));
            }
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
                null, shardCont, snapshotId.getUUID(), NamedXContentRegistry.EMPTY
            );
            shardSnapshots.add(new ShardSnapshot(shard, shardSnapshot));
        }

        return new ResolvedIndex(snapshotId, indexId, indexMetadata, shardSnapshots);
    }

    private static boolean wildcardMatch(String text, String pattern) {
        String regex = "^" + pattern.replace(".", "\\.").replace("*", ".*") + "$";
        return text.matches(regex);
    }

    public record ShardSnapshot(int shardId, BlobStoreIndexShardSnapshot snapshot) {}

    public record ResolvedIndex(SnapshotId snapshotId, IndexId indexId, IndexMetadata indexMetadata, List<ShardSnapshot> shardSnapshots) {}
}
