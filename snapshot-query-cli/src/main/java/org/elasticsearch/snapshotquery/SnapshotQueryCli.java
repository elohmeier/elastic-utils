package org.elasticsearch.snapshotquery;

import joptsimple.OptionSet;
import joptsimple.OptionSpec;

import org.apache.lucene.index.DirectoryReader;
import org.apache.lucene.index.StoredFields;
import org.apache.lucene.search.IndexSearcher;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.ScoreDoc;
import org.apache.lucene.search.TopDocs;
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
import org.elasticsearch.xcontent.XContentBuilder;
import org.elasticsearch.xcontent.XContentType;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.List;

public class SnapshotQueryCli extends Command {

    private final S3Options s3Options;
    private final OptionSpec<String> snapshotOption;
    private final OptionSpec<String> indexOption;
    private final OptionSpec<String> queryFileOption;
    private final OptionSpec<String> queryOption;
    private final OptionSpec<Integer> sizeOption;

    public SnapshotQueryCli() {
        super("Query Elasticsearch snapshot data in S3 without a running cluster");
        s3Options = new S3Options(parser);
        snapshotOption = parser.acceptsAll(Arrays.asList("s", "snapshot"), "Snapshot name").withRequiredArg().required();
        indexOption = parser.acceptsAll(Arrays.asList("i", "index"), "Index name").withRequiredArg().required();
        queryFileOption = parser.accepts("query-file", "Path to JSON file containing Query DSL").withRequiredArg();
        queryOption = parser.acceptsAll(Arrays.asList("q", "query"), "Inline Query DSL JSON").withRequiredArg();
        sizeOption = parser.accepts("size", "Maximum number of results").withRequiredArg().ofType(Integer.class).defaultsTo(10);
    }

    @Override
    protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo) throws Exception {
        long startTime = System.currentTimeMillis();

        String snapshotName = snapshotOption.value(options);
        String indexName = indexOption.value(options);
        int size = sizeOption.value(options);

        String queryJson = resolveQuery(options);

        terminal.errorPrintln("Connecting to S3 bucket: " + s3Options.bucket(options));
        terminal.errorPrintln("Snapshot: " + snapshotName + ", Index: " + indexName);

        // Step 1: Create S3 blob store
        try (S3ClientFactory.S3Access s3Access = s3Options.connect(options)) {
            BlobContainer rootContainer = s3Access.rootContainer();

            // Step 2: Load snapshot metadata
            terminal.errorPrintln("Loading snapshot metadata...");
            SnapshotMetadataLoader metadataLoader = new SnapshotMetadataLoader(rootContainer, s3Access);
            SnapshotMetadataLoader.ResolvedIndex resolved = metadataLoader.resolve(snapshotName, indexName);
            terminal.errorPrintln(
                "Found index [" + indexName + "] with " + resolved.shardSnapshots().size() + " shard(s)"
            );

            // Step 3: Parse query
            Query luceneQuery = SimpleQueryTranslator.translate(queryJson);
            terminal.errorPrintln("Query: " + luceneQuery);

            // Step 4: Execute across all shards
            long totalHits = 0;
            TopDocs[] allTopDocs = new TopDocs[resolved.shardSnapshots().size()];
            IndexSearcher[] searchers = new IndexSearcher[resolved.shardSnapshots().size()];
            DirectoryReader[] readers = new DirectoryReader[resolved.shardSnapshots().size()];
            Directory[] directories = new Directory[resolved.shardSnapshots().size()];

            try {
                List<SnapshotMetadataLoader.ShardSnapshot> shardSnapshots = resolved.shardSnapshots();
                for (int shard = 0; shard < shardSnapshots.size(); shard++) {
                    SnapshotMetadataLoader.ShardSnapshot shardSnapshot = shardSnapshots.get(shard);
                    BlobContainer shardContainer = metadataLoader.shardContainer(resolved.indexId(), shard);
                    BlobStoreIndexShardSnapshot blobStoreSnapshot = shardSnapshot.snapshot();

                    directories[shard] = new SnapshotQueryDirectory(shardContainer, blobStoreSnapshot);
                    readers[shard] = DirectoryReader.open(directories[shard]);
                    searchers[shard] = new IndexSearcher(readers[shard]);

                    allTopDocs[shard] = searchers[shard].search(luceneQuery, size);
                    totalHits += allTopDocs[shard].totalHits.value();
                }

                // Step 5: Merge results and output
                long tookMs = System.currentTimeMillis() - startTime;
                outputResults(terminal, allTopDocs, searchers, indexName, size, totalHits, tookMs);
            } finally {
                for (DirectoryReader reader : readers) {
                    if (reader != null) reader.close();
                }
                for (Directory dir : directories) {
                    if (dir != null) dir.close();
                }
            }
        }
    }

    private String resolveQuery(OptionSet options) throws UserException, IOException {
        if (options.has(queryFileOption)) {
            return readQueryFile(queryFileOption.value(options));
        } else if (options.has(queryOption)) {
            return queryOption.value(options);
        } else {
            throw new UserException(ExitCodes.USAGE, "Either --query or --query-file must be specified");
        }
    }

    @SuppressForbidden(reason = "file arg for cli")
    private static String readQueryFile(String path) throws IOException {
        return Files.readString(PathUtils.get(path));
    }

    private static void outputResults(
        Terminal terminal,
        TopDocs[] allTopDocs,
        IndexSearcher[] searchers,
        String indexName,
        int size,
        long totalHits,
        long tookMs
    ) throws IOException {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (XContentBuilder builder = new XContentBuilder(XContentType.JSON.xContent(), baos)) {
            builder.prettyPrint();
            builder.startObject();
            builder.field("took_ms", tookMs);
            builder.startObject("hits");
            builder.field("total", totalHits);
            builder.startArray("hits");

            int remaining = size;
            for (int shard = 0; shard < allTopDocs.length && remaining > 0; shard++) {
                StoredFields storedFields = searchers[shard].storedFields();
                for (ScoreDoc scoreDoc : allTopDocs[shard].scoreDocs) {
                    if (remaining-- <= 0) break;
                    builder.startObject();
                    builder.field("_index", indexName);
                    builder.field("_shard", shard);
                    builder.field("_score", scoreDoc.score);

                    var doc = storedFields.document(scoreDoc.doc);
                    var sourceField = doc.getBinaryValue("_source");
                    if (sourceField != null) {
                        builder.rawField(
                            "_source",
                            new java.io.ByteArrayInputStream(sourceField.bytes, sourceField.offset, sourceField.length),
                            XContentType.JSON
                        );
                    }
                    builder.endObject();
                }
            }

            builder.endArray();
            builder.endObject();
            builder.endObject();
        }
        terminal.println(baos.toString());
    }
}
