package org.elasticsearch.snapshotquery;

import joptsimple.OptionSet;
import joptsimple.OptionSpec;

import org.elasticsearch.cli.Command;
import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.common.blobstore.BlobContainer;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.Arrays;
import java.util.List;

public class ListSnapshotsCli extends Command {

    private static final DateTimeFormatter FMT = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(ZoneId.of("UTC"));

    private final OptionSpec<String> bucketOption;
    private final OptionSpec<String> basePathOption;
    private final OptionSpec<String> regionOption;
    private final OptionSpec<String> endpointOption;
    private final OptionSpec<String> accessKeyOption;
    private final OptionSpec<String> secretKeyOption;
    private final OptionSpec<Void> trustAllCertsOption;
    private final OptionSpec<String> resolveOption;
    private final OptionSpec<String> indexOption;

    public ListSnapshotsCli() {
        super("List snapshots in an S3 snapshot repository");
        bucketOption = parser.acceptsAll(Arrays.asList("b", "bucket"), "S3 bucket name").withRequiredArg().required();
        basePathOption = parser.accepts("base-path", "Base path within the S3 bucket").withRequiredArg().defaultsTo("");
        regionOption = parser.accepts("region", "AWS region").withRequiredArg().defaultsTo("us-east-1");
        endpointOption = parser.accepts("endpoint", "Custom S3 endpoint URL").withRequiredArg();
        accessKeyOption = parser.accepts("access-key", "AWS access key").withRequiredArg();
        secretKeyOption = parser.accepts("secret-key", "AWS secret key").withRequiredArg();
        trustAllCertsOption = parser.accepts("trust-all-certs", "Disable TLS certificate verification");
        resolveOption = parser.accepts("resolve", "Resolve hostname to IP (hostname:ip)").withRequiredArg();
        indexOption = parser.acceptsAll(Arrays.asList("i", "index"), "Filter: only show snapshots containing this index or alias").withRequiredArg();
    }

    @Override
    protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo) throws Exception {
        String bucket = bucketOption.value(options);
        String basePath = basePathOption.value(options);
        String region = regionOption.value(options);
        String endpoint = options.has(endpointOption) ? endpointOption.value(options) : null;
        String accessKey = options.has(accessKeyOption) ? accessKeyOption.value(options) : null;
        String secretKey = options.has(secretKeyOption) ? secretKeyOption.value(options) : null;
        boolean trustAllCerts = options.has(trustAllCertsOption);
        String resolve = options.has(resolveOption) ? resolveOption.value(options) : null;
        String indexFilter = options.has(indexOption) ? indexOption.value(options) : null;

        try (S3ClientFactory.S3Access s3Access = S3ClientFactory.create(bucket, basePath, region, endpoint, accessKey, secretKey, trustAllCerts, resolve)) {
            BlobContainer rootContainer = s3Access.rootContainer();
            SnapshotMetadataLoader loader = new SnapshotMetadataLoader(rootContainer, s3Access);

            if (indexFilter != null) {
                terminal.errorPrintln("Finding snapshots containing index/alias [" + indexFilter + "]...");
                List<String> snapshots = loader.findSnapshotsForIndex(indexFilter);
                if (snapshots.isEmpty()) {
                    terminal.errorPrintln("No snapshots found containing [" + indexFilter + "]");
                    return;
                }
                terminal.println("Snapshots containing [" + indexFilter + "] (newest first):");
                for (String name : snapshots) {
                    terminal.println("  " + name);
                }
            } else {
                terminal.errorPrintln("Loading snapshot list...");
                List<SnapshotMetadataLoader.SnapshotSummary> snapshots = loader.listSnapshots();
                if (snapshots.isEmpty()) {
                    terminal.errorPrintln("No snapshots found in repository");
                    return;
                }
                // Header
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
