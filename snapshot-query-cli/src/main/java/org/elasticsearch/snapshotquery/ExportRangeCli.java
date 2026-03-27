package org.elasticsearch.snapshotquery;

import java.io.IOException;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.stream.Stream;
import joptsimple.OptionSet;
import joptsimple.OptionSpec;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.Sort;
import org.elasticsearch.cli.Command;
import org.elasticsearch.cli.ExitCodes;
import org.elasticsearch.cli.ProcessInfo;
import org.elasticsearch.cli.Terminal;
import org.elasticsearch.cli.UserException;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.core.PathUtils;
import org.elasticsearch.core.SuppressForbidden;

public class ExportRangeCli extends Command {

  private final S3Options s3Options;
  private final OptionSpec<String> indexOption;
  private final OptionSpec<String> indexDateFromOption;
  private final OptionSpec<String> indexDateToOption;
  private final OptionSpec<String> queryFileOption;
  private final OptionSpec<String> queryOption;
  private final OptionSpec<String> fromDateOption;
  private final OptionSpec<String> toDateOption;
  private final OptionSpec<String> outputDirOption;
  private final OptionSpec<String> compressionOption;
  private final OptionSpec<Integer> batchSizeOption;
  private final OptionSpec<Void> allSnapshotsOption;
  private final OptionSpec<String> profileFileOption;
  private final OptionSpec<Integer> parallelShardsOption;
  private final OptionSpec<Integer> parallelIndicesOption;
  private final OptionSpec<Void> resumeOption;

  public ExportRangeCli() {
    super("Export many snapshot/index pairs for an index date range in a single JVM");
    s3Options = new S3Options(parser);
    indexOption =
        parser
            .acceptsAll(Arrays.asList("i", "index"), "Index name, alias, or pattern")
            .withRequiredArg()
            .required();
    indexDateFromOption =
        parser
            .accepts("index-date-from", "Inclusive index date filter (YYYY-MM-DD)")
            .withRequiredArg()
            .required();
    indexDateToOption =
        parser
            .accepts("index-date-to", "Inclusive index date filter (YYYY-MM-DD)")
            .withRequiredArg()
            .required();
    queryFileOption =
        parser
            .accepts("query-file", "Path to JSON file containing search body or Query DSL")
            .withRequiredArg();
    queryOption =
        parser.acceptsAll(Arrays.asList("q", "query"), "Inline Query DSL JSON").withRequiredArg();
    fromDateOption =
        parser
            .accepts(
                "from-date", "Start date filter for @timestamp (inclusive, ISO date or datetime)")
            .withRequiredArg();
    toDateOption =
        parser
            .accepts("to-date", "End date filter for @timestamp (exclusive, ISO date or datetime)")
            .withRequiredArg();
    outputDirOption =
        parser
            .acceptsAll(Arrays.asList("o", "output-dir"), "Directory for exported files")
            .withRequiredArg()
            .required();
    compressionOption =
        parser
            .accepts("compression", "Compression: none, zstd")
            .withRequiredArg()
            .defaultsTo("none");
    batchSizeOption =
        parser
            .accepts("batch-size", "Documents per search batch")
            .withRequiredArg()
            .ofType(Integer.class)
            .defaultsTo(10000);
    allSnapshotsOption =
        parser.accepts(
            "all-snapshots",
            "Export all matching snapshot/index pairs instead of only the newest snapshot per index");
    profileFileOption =
        parser
            .accepts("profile-file", "Write JSON profiling counters to this file")
            .withRequiredArg();
    parallelShardsOption =
        parser
            .accepts("parallel-shards", "Number of shards to export concurrently per index")
            .withRequiredArg()
            .ofType(Integer.class)
            .defaultsTo(3);
    parallelIndicesOption =
        parser
            .accepts("parallel-indices", "Number of indices to export concurrently")
            .withRequiredArg()
            .ofType(Integer.class)
            .defaultsTo(1);
    resumeOption =
        parser.accepts(
            "resume",
            "Skip indices whose output file already exists (for resuming interrupted exports)");
  }

  @Override
  protected void execute(Terminal terminal, OptionSet options, ProcessInfo processInfo)
      throws Exception {
    long startTime = System.currentTimeMillis();

    String indexPattern = indexOption.value(options);
    String indexDateFrom = indexDateFromOption.value(options);
    String indexDateTo = indexDateToOption.value(options);
    String fromDate = options.has(fromDateOption) ? fromDateOption.value(options) : null;
    String toDate = options.has(toDateOption) ? toDateOption.value(options) : null;
    String outputDir = outputDirOption.value(options);
    String compression = compressionOption.value(options);
    int batchSize = batchSizeOption.value(options);
    boolean allSnapshots = options.has(allSnapshotsOption);
    int parallelShards = parallelShardsOption.value(options);
    int parallelIndices = parallelIndicesOption.value(options);
    boolean resume = options.has(resumeOption);
    String profileFile = options.has(profileFileOption) ? profileFileOption.value(options) : null;
    ProfilingRecorder profilingRecorder = new ProfilingRecorder();
    ProfileFileWriter profileFileWriter = ProfileFileWriter.create(profileFile, profilingRecorder);
    ProgressReporter progressReporter = new ProgressReporter(terminal, profilingRecorder);

    long queryParseStartNanos = System.nanoTime();
    SearchBodyParser searchBody = parseSearchBody(options, fromDate, toDate);
    profilingRecorder.addPhaseNanos("query_parse", System.nanoTime() - queryParseStartNanos);
    Query luceneQuery = searchBody.query();
    Sort sort = searchBody.sort();
    List<String> sourceFields = searchBody.sourceFields();

    LocalDate minIndexDate = parseIndexDate(indexDateFromOption.value(options));
    LocalDate maxIndexDate = parseIndexDate(indexDateToOption.value(options));
    if (maxIndexDate.isBefore(minIndexDate)) {
      throw new UserException(
          ExitCodes.USAGE, "--index-date-to must be on or after --index-date-from");
    }

    ensureDirectory(outputDir);
    if (resume) {
      cleanupTempFiles(outputDir);
    }

    terminal.errorPrintln("Connecting to S3 bucket: " + s3Options.bucket(options));
    long connectStartNanos = System.nanoTime();
    try (S3ClientFactory.S3Access s3Access = s3Options.connect(options, profilingRecorder);
        ProgressReporter ignored = progressReporter) {
      profilingRecorder.addPhaseNanos("s3_connect", System.nanoTime() - connectStartNanos);
      BlobContainer rootContainer = s3Access.rootContainer();
      SnapshotMetadataLoader metadataLoader = new SnapshotMetadataLoader(rootContainer, s3Access);

      terminal.errorPrintln("Discovering snapshot/index pairs...");
      profilingRecorder.setCurrentStage("discovering");
      long discoveryStartNanos = System.nanoTime();
      List<SnapshotMetadataLoader.ExportTarget> targets =
          metadataLoader.findExportTargets(indexPattern, minIndexDate, maxIndexDate, !allSnapshots);
      profilingRecorder.addPhaseNanos("target_discovery", System.nanoTime() - discoveryStartNanos);

      if (targets.isEmpty()) {
        terminal.errorPrintln("No matching snapshot/index pairs found");
        profileFileWriter.flushCompleted();
        return;
      }

      terminal.errorPrintln("Found " + targets.size() + " snapshot/index pairs to export");
      profilingRecorder.setTargetTotals(0, targets.size());
      AtomicLong totalExported = new AtomicLong();
      AtomicInteger completed = new AtomicInteger();

      if (parallelIndices > 1 && targets.size() > 1) {
        exportIndicesParallel(
            terminal,
            metadataLoader,
            targets,
            luceneQuery,
            sort,
            sourceFields,
            batchSize,
            outputDir,
            compression,
            allSnapshots,
            startTime,
            profilingRecorder,
            progressReporter,
            parallelShards,
            parallelIndices,
            resume,
            totalExported,
            completed);
      } else {
        for (SnapshotMetadataLoader.ExportTarget target : targets) {
          int current = completed.get() + 1;
          String outputPath =
              buildOutputPath(outputDir, target, compression, allSnapshots, targets);

          if (resume && isAlreadyExported(outputPath)) {
            progressReporter.clearLine();
            terminal.errorPrintln(
                "["
                    + current
                    + "/"
                    + targets.size()
                    + "] Skipping "
                    + target.indexName()
                    + " (already exported)");
            completed.incrementAndGet();
            continue;
          }

          profilingRecorder.startTarget(
              target.snapshotName(), target.indexName(), current - 1, targets.size());
          progressReporter.clearLine();
          terminal.errorPrintln(
              "["
                  + current
                  + "/"
                  + targets.size()
                  + "] Exporting "
                  + target.indexName()
                  + " from "
                  + target.snapshotName()
                  + "...");

          try (SnapshotExportSupport.AtomicFileOutput atomicOut =
              SnapshotExportSupport.openAtomicOutput(outputPath, compression)) {
            long resolveStartNanos = System.nanoTime();
            SnapshotMetadataLoader.ResolvedIndex resolved =
                metadataLoader.resolve(target.snapshotName(), target.indexName());
            profilingRecorder.recordIndexResolve(
                target.indexName(), target.snapshotName(), System.nanoTime() - resolveStartNanos);
            long exportStartNanos = System.nanoTime();
            long exported =
                SnapshotExportSupport.exportResolvedIndex(
                    terminal,
                    metadataLoader,
                    resolved,
                    luceneQuery,
                    sort,
                    sourceFields,
                    batchSize,
                    atomicOut.stream(),
                    startTime,
                    profilingRecorder,
                    progressReporter,
                    parallelShards);
            atomicOut.commit();
            profilingRecorder.recordIndexExport(
                target.indexName(),
                target.snapshotName(),
                outputPath,
                exported,
                System.nanoTime() - exportStartNanos);
            totalExported.addAndGet(exported);
            int done = completed.incrementAndGet();
            profilingRecorder.finishTarget(target.indexName(), done, targets.size());
            progressReporter.clearLine();
            terminal.errorPrintln("  Index complete: " + exported + " docs -> " + outputPath);
          }
        }
      }

      long tookMs = System.currentTimeMillis() - startTime;
      progressReporter.clearLine();
      terminal.errorPrintln(
          "Export-range complete: "
              + totalExported.get()
              + " documents in "
              + (tookMs / 1000)
              + "s");
      profileFileWriter.flushCompleted();
    }
  }

  private static void exportIndicesParallel(
      Terminal terminal,
      SnapshotMetadataLoader metadataLoader,
      List<SnapshotMetadataLoader.ExportTarget> targets,
      Query luceneQuery,
      Sort sort,
      List<String> sourceFields,
      int batchSize,
      String outputDir,
      String compression,
      boolean allSnapshots,
      long startTimeMillis,
      ProfilingRecorder profilingRecorder,
      ProgressReporter progressReporter,
      int parallelShards,
      int parallelIndices,
      boolean resume,
      AtomicLong totalExported,
      AtomicInteger completed)
      throws IOException {
    ExecutorService executor =
        Executors.newFixedThreadPool(
            parallelIndices,
            r -> {
              Thread t = new Thread(r, "index-export");
              t.setDaemon(true);
              return t;
            });

    List<Future<Void>> futures = new ArrayList<>();
    for (SnapshotMetadataLoader.ExportTarget target : targets) {
      futures.add(
          executor.submit(
              () -> {
                int current = completed.get() + 1;
                String outputPath =
                    buildOutputPath(outputDir, target, compression, allSnapshots, targets);

                if (resume && isAlreadyExported(outputPath)) {
                  progressReporter.clearLine();
                  terminal.errorPrintln(
                      "["
                          + current
                          + "/"
                          + targets.size()
                          + "] Skipping "
                          + target.indexName()
                          + " (already exported)");
                  completed.incrementAndGet();
                  return null;
                }

                profilingRecorder.startTarget(
                    target.snapshotName(), target.indexName(), current - 1, targets.size());
                progressReporter.clearLine();
                terminal.errorPrintln(
                    "["
                        + current
                        + "/"
                        + targets.size()
                        + "] Exporting "
                        + target.indexName()
                        + " from "
                        + target.snapshotName()
                        + "...");

                try (SnapshotExportSupport.AtomicFileOutput atomicOut =
                    SnapshotExportSupport.openAtomicOutput(outputPath, compression)) {
                  long resolveStartNanos = System.nanoTime();
                  SnapshotMetadataLoader.ResolvedIndex resolved =
                      metadataLoader.resolve(target.snapshotName(), target.indexName());
                  profilingRecorder.recordIndexResolve(
                      target.indexName(),
                      target.snapshotName(),
                      System.nanoTime() - resolveStartNanos);
                  long exportStartNanos = System.nanoTime();
                  long exported =
                      SnapshotExportSupport.exportResolvedIndex(
                          terminal,
                          metadataLoader,
                          resolved,
                          luceneQuery,
                          sort,
                          sourceFields,
                          batchSize,
                          atomicOut.stream(),
                          startTimeMillis,
                          profilingRecorder,
                          progressReporter,
                          parallelShards);
                  atomicOut.commit();
                  profilingRecorder.recordIndexExport(
                      target.indexName(),
                      target.snapshotName(),
                      outputPath,
                      exported,
                      System.nanoTime() - exportStartNanos);
                  totalExported.addAndGet(exported);
                  int done = completed.incrementAndGet();
                  profilingRecorder.finishTarget(target.indexName(), done, targets.size());
                  progressReporter.clearLine();
                  terminal.errorPrintln("  Index complete: " + exported + " docs -> " + outputPath);
                }
                return null;
              }));
    }

    executor.shutdown();
    IOException failure = null;
    for (Future<Void> future : futures) {
      try {
        future.get();
      } catch (ExecutionException e) {
        executor.shutdownNow();
        Throwable cause = e.getCause();
        if (cause instanceof IOException ioe) {
          failure = ioe;
        } else {
          failure = new IOException("Index export failed", cause);
        }
        break;
      } catch (InterruptedException e) {
        executor.shutdownNow();
        Thread.currentThread().interrupt();
        failure = new IOException("Index export interrupted", e);
        break;
      }
    }
    if (failure != null) {
      throw failure;
    }
  }

  private SearchBodyParser parseSearchBody(OptionSet options, String fromDate, String toDate)
      throws IOException, UserException {
    String json;
    if (options.has(queryFileOption)) {
      json = readFile(queryFileOption.value(options));
    } else if (options.has(queryOption)) {
      json = queryOption.value(options);
    } else {
      throw new UserException(ExitCodes.USAGE, "Either --query or --query-file must be specified");
    }

    if (SnapshotExportCli.isFullSearchBody(json)) {
      return SearchBodyParser.parse(json, fromDate, toDate);
    }
    return SearchBodyParser.fromQueryOnly(json, fromDate, toDate);
  }

  private static LocalDate parseIndexDate(String value) {
    try {
      return LocalDate.parse(value, DateTimeFormatter.ISO_LOCAL_DATE);
    } catch (DateTimeParseException e) {
      throw new IllegalArgumentException("Invalid index date: " + value + ". Expected YYYY-MM-DD");
    }
  }

  private static String buildOutputPath(
      String outputDir,
      SnapshotMetadataLoader.ExportTarget target,
      String compression,
      boolean allSnapshots,
      List<SnapshotMetadataLoader.ExportTarget> allTargets) {
    boolean duplicateIndexName =
        allTargets.stream().filter(t -> t.indexName().equals(target.indexName())).count() > 1;
    String baseName = target.indexName();
    if (allSnapshots || duplicateIndexName) {
      baseName = baseName + "__" + target.snapshotName();
    }
    String extension = "zstd".equalsIgnoreCase(compression) ? ".jsonl.zst" : ".jsonl";
    return PathUtils.get(outputDir, sanitizeFileName(baseName) + extension).toString();
  }

  private static String sanitizeFileName(String value) {
    return value.replace('/', '_');
  }

  @SuppressForbidden(reason = "file arg for cli")
  private static String readFile(String path) throws IOException {
    return Files.readString(PathUtils.get(path));
  }

  @SuppressForbidden(reason = "check output file for resume")
  private static boolean isAlreadyExported(String path) throws IOException {
    Path p = PathUtils.get(path);
    return Files.exists(p) && Files.size(p) > 0;
  }

  @SuppressForbidden(reason = "cleanup temp files for resume")
  private static void cleanupTempFiles(String outputDir) throws IOException {
    Path dir = PathUtils.get(outputDir);
    if (!Files.isDirectory(dir)) {
      return;
    }
    try (Stream<Path> stream = Files.list(dir)) {
      stream
          .filter(p -> p.toString().endsWith(".tmp"))
          .forEach(
              p -> {
                try {
                  Files.deleteIfExists(p);
                } catch (IOException ignored) {
                }
              });
    }
  }

  @SuppressForbidden(reason = "create output directory for cli")
  private static void ensureDirectory(String outputDir) throws IOException {
    Path path = PathUtils.get(outputDir);
    Files.createDirectories(path);
  }

  private static final class ProfileFileWriter {
    private final Path outputPath;
    private final ProfilingRecorder profilingRecorder;
    private final AtomicBoolean flushed = new AtomicBoolean();
    private final Thread shutdownHook;

    private ProfileFileWriter(Path outputPath, ProfilingRecorder profilingRecorder) {
      this.outputPath = outputPath;
      this.profilingRecorder = profilingRecorder;
      this.shutdownHook =
          new Thread(
              () -> {
                try {
                  flushInterrupted();
                } catch (IOException ignored) {
                  // best effort during shutdown
                }
              },
              "snapshot-query-profile-shutdown-hook");
    }

    static ProfileFileWriter create(String profileFile, ProfilingRecorder profilingRecorder) {
      if (profileFile == null || profilingRecorder == null) {
        return new ProfileFileWriter(null, null);
      }
      ProfileFileWriter writer =
          new ProfileFileWriter(PathUtils.get(profileFile), profilingRecorder);
      Runtime.getRuntime().addShutdownHook(writer.shutdownHook);
      return writer;
    }

    void flushCompleted() throws IOException {
      if (profilingRecorder == null || outputPath == null || !flushed.compareAndSet(false, true)) {
        return;
      }
      profilingRecorder.markCompleted();
      writeAtomically();
      removeShutdownHook();
    }

    void flushInterrupted() throws IOException {
      if (profilingRecorder == null || outputPath == null || !flushed.compareAndSet(false, true)) {
        return;
      }
      profilingRecorder.markInterrupted();
      writeAtomically();
    }

    @SuppressForbidden(reason = "write profile file for cli")
    private void writeAtomically() throws IOException {
      Path parent = outputPath.getParent();
      if (parent != null) {
        Files.createDirectories(parent);
      }
      Path tempFile = outputPath.resolveSibling(outputPath.getFileName() + ".tmp");
      Files.writeString(tempFile, profilingRecorder.renderJson());
      try {
        Files.move(
            tempFile,
            outputPath,
            StandardCopyOption.REPLACE_EXISTING,
            StandardCopyOption.ATOMIC_MOVE);
      } catch (AtomicMoveNotSupportedException e) {
        Files.move(tempFile, outputPath, StandardCopyOption.REPLACE_EXISTING);
      }
    }

    private void removeShutdownHook() {
      try {
        Runtime.getRuntime().removeShutdownHook(shutdownHook);
      } catch (IllegalStateException ignored) {
        // JVM is already shutting down
      }
    }
  }
}
