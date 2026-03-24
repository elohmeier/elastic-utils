package org.elasticsearch.snapshotquery;

import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;

/**
 * Standalone entry point for the snapshot-query CLI.
 * Delegates to the ES CLI framework's Command.main().
 */
public class SnapshotQueryMain {
    public static void main(String[] args) throws Exception {
        var command = new SnapshotQueryCli();
        command.main(args, Terminal.DEFAULT, ProcessInfo.fromSystem());
    }
}
