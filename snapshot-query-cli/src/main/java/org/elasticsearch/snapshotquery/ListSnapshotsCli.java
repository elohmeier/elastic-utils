package org.elasticsearch.snapshotquery;

import joptsimple.OptionSet;
import joptsimple.OptionSpec;

import org.elasticsearch.cli.Command;
import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.xcontent.XContentBuilder;
import org.elasticsearch.xcontent.XContentType;

import java.io.ByteArrayOutputStream;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Arrays;
import java.util.List;

public class ListSnapshotsCli extends Command {

    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(ZoneId.of("UTC"));

    private final S3Options s3Options;
    private final OptionSpec<String> indexOption;
    private final OptionSpec<Void> jsonOption;

    public ListSnapshotsCli() {
        super("List snapshots in an S3 snapshot repository");
        s3Options = new S3Options(parser);
        indexOption = parser.acceptsAll(Arrays.asList("i", "index"), "Filter: only show snapshots containing this index or alias").withRequiredArg();
        jsonOption = parser.accepts("json", "Emit machine-readable JSON");
    }

    @Override
    protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo) throws Exception {
        String indexFilter = options.has(indexOption) ? indexOption.value(options) : null;

        try (S3ClientFactory.S3Access s3Access = s3Options.connect(options)) {
            BlobContainer rootContainer = s3Access.rootContainer();
            SnapshotMetadataLoader loader = new SnapshotMetadataLoader(rootContainer, s3Access);

            if (indexFilter != null) {
                terminal.errorPrintln("Finding snapshots containing index/alias [" + indexFilter + "]...");
                List<SnapshotMetadataLoader.SnapshotSummary> snapshots = loader.listSnapshotsForIndex(indexFilter);
                if (snapshots.isEmpty()) {
                    terminal.errorPrintln("No snapshots found containing [" + indexFilter + "]");
                    return;
                }
                if (options.has(jsonOption)) {
                    terminal.println(renderJson(snapshots));
                } else {
                    terminal.println("Snapshots containing [" + indexFilter + "] (newest first):");
                    for (SnapshotMetadataLoader.SnapshotSummary snapshot : snapshots) {
                        terminal.println("  " + snapshot.snapshotId().getName());
                    }
                }
            } else {
                terminal.errorPrintln("Loading snapshot list...");
                List<SnapshotMetadataLoader.SnapshotSummary> snapshots = loader.listSnapshots();
                if (snapshots.isEmpty()) {
                    terminal.errorPrintln("No snapshots found in repository");
                    return;
                }
                if (options.has(jsonOption)) {
                    terminal.println(renderJson(snapshots));
                } else {
                    terminal.println(String.format("%-40s  %-10s  %-20s  %-20s  %s", "SNAPSHOT", "STATE", "START", "END", "INDICES"));
                    terminal.println("-".repeat(130));
                    for (var snap : snapshots) {
                        String start = snap.startTimeMillis() > 0 ? FMT.format(Instant.ofEpochMilli(snap.startTimeMillis())) : "n/a";
                        String end = snap.endTimeMillis() > 0 ? FMT.format(Instant.ofEpochMilli(snap.endTimeMillis())) : "n/a";
                        String indices = snap.indices().size() <= 5
                            ? String.join(", ", snap.indices())
                            : snap.indices().size() + " indices";
                        terminal.println(String.format("%-40s  %-10s  %-20s  %-20s  %s",
                            snap.snapshotId().getName(), snap.state(), start, end, indices));
                    }
                    terminal.errorPrintln("\nTotal: " + snapshots.size() + " snapshot(s)");
                }
            }
        }
    }

    private static String renderJson(List<SnapshotMetadataLoader.SnapshotSummary> snapshots) throws Exception {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (XContentBuilder builder = new XContentBuilder(XContentType.JSON.xContent(), baos)) {
            builder.prettyPrint();
            builder.startArray();
            for (SnapshotMetadataLoader.SnapshotSummary snap : snapshots) {
                builder.startObject();
                builder.field("snapshot", snap.snapshotId().getName());
                builder.field("state", snap.state());
                builder.field("start_time_millis", snap.startTimeMillis());
                builder.field("end_time_millis", snap.endTimeMillis());
                if (snap.startTimeMillis() > 0) {
                    builder.field("start_time", Instant.ofEpochMilli(snap.startTimeMillis()).toString());
                }
                if (snap.endTimeMillis() > 0) {
                    builder.field("end_time", Instant.ofEpochMilli(snap.endTimeMillis()).toString());
                }
                builder.array("indices", snap.indices().toArray(String[]::new));
                builder.endObject();
            }
            builder.endArray();
        }
        return baos.toString();
    }
}
