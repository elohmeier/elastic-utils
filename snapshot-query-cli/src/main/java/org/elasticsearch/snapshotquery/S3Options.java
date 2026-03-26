package org.elasticsearch.snapshotquery;

import java.util.Arrays;
import joptsimple.OptionParser;
import joptsimple.OptionSet;
import joptsimple.OptionSpec;
import org.elasticsearch.core.Nullable;

/**
 * Shared S3 connection options with environment variable fallback.
 *
 * <p>Environment variables: S3_BUCKET, S3_BASE_PATH, S3_REGION, S3_ENDPOINT, S3_ACCESS_KEY (or
 * AWS_ACCESS_KEY_ID), S3_SECRET_KEY (or AWS_SECRET_ACCESS_KEY), S3_TRUST_ALL_CERTS, S3_RESOLVE
 */
public class S3Options {

  final OptionSpec<String> bucket;
  final OptionSpec<String> basePath;
  final OptionSpec<String> region;
  final OptionSpec<String> endpoint;
  final OptionSpec<String> accessKey;
  final OptionSpec<String> secretKey;
  final OptionSpec<Void> trustAllCerts;
  final OptionSpec<String> resolve;

  public S3Options(OptionParser parser) {
    bucket =
        parser
            .acceptsAll(Arrays.asList("b", "bucket"), "S3 bucket name (env: S3_BUCKET)")
            .withRequiredArg();
    basePath =
        parser.accepts("base-path", "Base path in S3 bucket (env: S3_BASE_PATH)").withRequiredArg();
    region = parser.accepts("region", "AWS region (env: S3_REGION)").withRequiredArg();
    endpoint =
        parser.accepts("endpoint", "Custom S3 endpoint URL (env: S3_ENDPOINT)").withRequiredArg();
    accessKey =
        parser
            .accepts("access-key", "AWS access key (env: S3_ACCESS_KEY or AWS_ACCESS_KEY_ID)")
            .withRequiredArg();
    secretKey =
        parser
            .accepts("secret-key", "AWS secret key (env: S3_SECRET_KEY or AWS_SECRET_ACCESS_KEY)")
            .withRequiredArg();
    trustAllCerts =
        parser.accepts(
            "trust-all-certs", "Disable TLS certificate verification (env: S3_TRUST_ALL_CERTS)");
    resolve =
        parser
            .accepts("resolve", "Resolve hostname to IP, hostname:ip (env: S3_RESOLVE)")
            .withRequiredArg();
  }

  public S3ClientFactory.S3Access connect(OptionSet options) {
    return connect(options, null);
  }

  public S3ClientFactory.S3Access connect(
      OptionSet options, @Nullable ProfilingRecorder profilingRecorder) {
    String bucketVal = resolve(options, bucket, "S3_BUCKET", null);
    if (bucketVal == null || bucketVal.isEmpty()) {
      throw new IllegalArgumentException("--bucket or S3_BUCKET is required");
    }
    String basePathVal = resolve(options, basePath, "S3_BASE_PATH", "");
    String regionVal = resolve(options, region, "S3_REGION", "us-east-1");
    String endpointVal = resolve(options, endpoint, "S3_ENDPOINT", null);
    String accessKeyVal = resolve(options, accessKey, "S3_ACCESS_KEY", env("AWS_ACCESS_KEY_ID"));
    String secretKeyVal =
        resolve(options, secretKey, "S3_SECRET_KEY", env("AWS_SECRET_ACCESS_KEY"));
    boolean trustAll =
        options.has(trustAllCerts)
            || "1".equals(env("S3_TRUST_ALL_CERTS"))
            || "true".equalsIgnoreCase(env("S3_TRUST_ALL_CERTS"));
    String resolveVal = resolve(options, this.resolve, "S3_RESOLVE", null);

    return S3ClientFactory.create(
        bucketVal,
        basePathVal,
        regionVal,
        endpointVal,
        accessKeyVal,
        secretKeyVal,
        trustAll,
        resolveVal,
        profilingRecorder);
  }

  public String bucket(OptionSet options) {
    return resolve(options, bucket, "S3_BUCKET", null);
  }

  private static String resolve(
      OptionSet options, OptionSpec<String> spec, String envVar, String defaultVal) {
    if (options.has(spec)) {
      return spec.value(options);
    }
    String envVal = env(envVar);
    if (envVal != null && !envVal.isEmpty()) {
      return envVal;
    }
    return defaultVal;
  }

  private static String env(String name) {
    return System.getenv(name);
  }
}
