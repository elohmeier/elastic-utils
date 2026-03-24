package org.elasticsearch.snapshotquery;

import org.elasticsearch.cli.CliToolProvider;
import org.elasticsearch.cli.Command;

public class SnapshotQueryCliProvider implements CliToolProvider {
    @Override
    public String name() {
        return "snapshot-query";
    }

    @Override
    public Command create() {
        return new SnapshotQueryCli();
    }
}
