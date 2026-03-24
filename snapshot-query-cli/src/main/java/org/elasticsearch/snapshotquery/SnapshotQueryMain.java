package org.elasticsearch.snapshotquery;

import org.elasticsearch.cli.MultiCommand;
import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.logging.Level;
import org.elasticsearch.logging.Logger;
import org.elasticsearch.logging.internal.spi.LoggerFactory;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Proxy;

/**
 * Standalone entry point for the snapshot CLI tools.
 * Subcommands: query, export
 */
public class SnapshotQueryMain extends MultiCommand {

    SnapshotQueryMain() {
        super("Elasticsearch snapshot query and export tools");
        subcommands.put("query", new SnapshotQueryCli());
        subcommands.put("export", new SnapshotExportCli());
        subcommands.put("export-range", new ExportRangeCli());
        subcommands.put("snapshots", new ListSnapshotsCli());
    }

    public static void main(String[] args) throws Exception {
        configureBootstrapLogging();

        // Initialize a no-op LoggerFactory to prevent NPE in Command.mainWithoutErrorHandling
        if (LoggerFactory.provider() == null) {
            Logger noopLogger = (Logger) Proxy.newProxyInstance(
                Logger.class.getClassLoader(),
                new Class<?>[] { Logger.class },
                (proxy, method, methodArgs) -> {
                    Class<?> returnType = method.getReturnType();
                    if (returnType == boolean.class) return false;
                    if (returnType == String.class) return "noop";
                    return null;
                }
            );
            LoggerFactory.setInstance(new LoggerFactory() {
                @Override public Logger getLogger(String name) { return noopLogger; }
                @Override public Logger getLogger(Class<?> clazz) { return noopLogger; }
                @Override public void setRootLevel(Level level) {}
                @Override public Level getRootLevel() { return Level.INFO; }
            });
        }

        var command = new SnapshotQueryMain();
        command.main(args, Terminal.DEFAULT, ProcessInfo.fromSystem());
    }

    private static void configureBootstrapLogging() {
        // Log4j may initialize its simple/status logger before the real config is applied.
        // Force a minimal bootstrap format so Elasticsearch/Log4j startup does not try to
        // interpret a full logging pattern as a date-time format.
        System.setProperty("org.apache.logging.log4j.simplelog.showdatetime", "false");
        System.setProperty("org.apache.logging.log4j.simplelog.showContextMap", "false");
        System.setProperty("org.apache.logging.log4j.simplelog.showlogname", "false");
        System.setProperty("org.apache.logging.log4j.simplelog.showShortLogname", "true");
        System.setProperty("org.apache.logging.log4j.simplelog.level", "ERROR");
    }
}
