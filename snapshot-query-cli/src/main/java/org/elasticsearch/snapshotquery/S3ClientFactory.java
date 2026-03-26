package org.elasticsearch.snapshotquery;

import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.http.apache.ApacheHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3ClientBuilder;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Request;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Response;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;
import software.amazon.awssdk.services.s3.model.S3Object;

import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.common.blobstore.BlobPath;
import org.elasticsearch.common.blobstore.OperationPurpose;
import org.elasticsearch.common.blobstore.support.BlobMetadata;
import org.elasticsearch.common.bytes.BytesReference;
import org.elasticsearch.core.Nullable;

import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.URI;
import java.net.UnknownHostException;
import java.nio.file.NoSuchFileException;
import java.util.HashMap;
import java.util.Map;

import org.apache.http.conn.DnsResolver;

/**
 * Creates a minimal S3 client and BlobContainer for standalone snapshot reading.
 */
public class S3ClientFactory {

    public static S3Access create(
        String bucket,
        String basePath,
        String region,
        @Nullable String endpoint,
        @Nullable String accessKey,
        @Nullable String secretKey,
        boolean trustAllCerts,
        @Nullable String resolve,
        @Nullable ProfilingRecorder profilingRecorder
    ) {
        AwsCredentialsProvider credentialsProvider;
        if (accessKey != null && secretKey != null) {
            credentialsProvider = StaticCredentialsProvider.create(AwsBasicCredentials.create(accessKey, secretKey));
        } else {
            credentialsProvider = DefaultCredentialsProvider.create();
        }

        S3ClientBuilder builder = S3Client.builder().region(Region.of(region)).credentialsProvider(credentialsProvider);

        if (endpoint != null) {
            builder.endpointOverride(URI.create(endpoint));
            builder.forcePathStyle(true); // needed for MinIO and other S3-compatible stores
        }

        ApacheHttpClient.Builder httpBuilder = ApacheHttpClient.builder().maxConnections(100);

        if (trustAllCerts) {
            httpBuilder.tlsTrustManagersProvider(() -> new javax.net.ssl.TrustManager[] {
                new javax.net.ssl.X509TrustManager() {
                    public java.security.cert.X509Certificate[] getAcceptedIssuers() { return new java.security.cert.X509Certificate[0]; }
                    public void checkClientTrusted(java.security.cert.X509Certificate[] certs, String authType) {}
                    public void checkServerTrusted(java.security.cert.X509Certificate[] certs, String authType) {}
                }
            });
        }

        if (resolve != null) {
            // Format: "hostname:ip" (e.g. "s3-01.example.com:127.0.0.1")
            String[] parts = resolve.split(":", 2);
            String resolveHost = parts[0];
            String resolveIp = parts[1];
            httpBuilder.dnsResolver(new DnsResolver() {
                @Override
                public InetAddress[] resolve(String host) throws UnknownHostException {
                    if (host.equals(resolveHost)) {
                        return new InetAddress[] { InetAddress.getByName(resolveIp) };
                    }
                    return InetAddress.getAllByName(host);
                }
            });
        }

        builder.httpClient(httpBuilder.build());

        S3Client client = builder.build();
        BlobPath rootPath = basePath.isEmpty() ? BlobPath.EMPTY : BlobPath.EMPTY.add(basePath);
        return new S3Access(client, bucket, rootPath, profilingRecorder);
    }

    public static class S3Access implements Closeable {
        private final S3Client client;
        private final String bucket;
        private final BlobPath rootPath;
        private final ProfilingRecorder profilingRecorder;

        S3Access(S3Client client, String bucket, BlobPath rootPath, @Nullable ProfilingRecorder profilingRecorder) {
            this.client = client;
            this.bucket = bucket;
            this.rootPath = rootPath;
            this.profilingRecorder = profilingRecorder;
        }

        public BlobContainer rootContainer() {
            return containerFor(rootPath);
        }

        public BlobContainer containerFor(BlobPath path) {
            return new SimpleS3BlobContainer(client, bucket, path, profilingRecorder);
        }

        @Override
        public void close() {
            client.close();
        }
    }

    /**
     * Minimal read-only BlobContainer backed by S3Client.
     * Only implements the methods needed for snapshot metadata reading and Lucene index access.
     */
    static class SimpleS3BlobContainer implements BlobContainer {
        private final S3Client client;
        private final String bucket;
        private final BlobPath path;
        private final ProfilingRecorder profilingRecorder;

        SimpleS3BlobContainer(S3Client client, String bucket, BlobPath path, @Nullable ProfilingRecorder profilingRecorder) {
            this.client = client;
            this.bucket = bucket;
            this.path = path;
            this.profilingRecorder = profilingRecorder;
        }

        private String blobKey(String blobName) {
            String prefix = path.buildAsString();
            return prefix.isEmpty() ? blobName : prefix + blobName;
        }

        @Override
        public BlobPath path() {
            return path;
        }

        @Override
        public boolean blobExists(OperationPurpose purpose, String blobName) throws IOException {
            long startNanos = System.nanoTime();
            try {
                client.headObject(b -> b.bucket(bucket).key(blobKey(blobName)));
                if (profilingRecorder != null) {
                    profilingRecorder.recordS3Head(System.nanoTime() - startNanos);
                }
                return true;
            } catch (NoSuchKeyException e) {
                if (profilingRecorder != null) {
                    profilingRecorder.recordS3Head(System.nanoTime() - startNanos);
                }
                return false;
            }
        }

        @Override
        public InputStream readBlob(OperationPurpose purpose, String blobName) throws IOException {
            long startNanos = System.nanoTime();
            try {
                GetObjectRequest request = GetObjectRequest.builder().bucket(bucket).key(blobKey(blobName)).build();
                InputStream inputStream = client.getObject(request);
                if (profilingRecorder == null) {
                    return inputStream;
                }
                return new ProfilingInputStream(inputStream, profilingRecorder, false, -1, startNanos);
            } catch (NoSuchKeyException e) {
                throw new NoSuchFileException("Blob [" + blobKey(blobName) + "] not found in bucket [" + bucket + "]");
            }
        }

        @Override
        public InputStream readBlob(OperationPurpose purpose, String blobName, long position, long length) throws IOException {
            long startNanos = System.nanoTime();
            try {
                String range = "bytes=" + position + "-" + (position + length - 1);
                GetObjectRequest request = GetObjectRequest.builder().bucket(bucket).key(blobKey(blobName)).range(range).build();
                InputStream inputStream = client.getObject(request);
                if (profilingRecorder == null) {
                    return inputStream;
                }
                return new ProfilingInputStream(inputStream, profilingRecorder, true, length, startNanos);
            } catch (NoSuchKeyException e) {
                throw new NoSuchFileException("Blob [" + blobKey(blobName) + "] not found in bucket [" + bucket + "]");
            }
        }

        @Override
        public long readBlobPreferredLength() {
            return 32 * 1024 * 1024; // 32MB
        }

        @Override
        public Map<String, BlobMetadata> listBlobsByPrefix(OperationPurpose purpose, @Nullable String blobNamePrefix) throws IOException {
            String prefix = blobKey(blobNamePrefix != null ? blobNamePrefix : "");
            Map<String, BlobMetadata> blobs = new HashMap<>();
            String continuationToken = null;

            do {
                long startNanos = System.nanoTime();
                ListObjectsV2Request.Builder reqBuilder = ListObjectsV2Request.builder().bucket(bucket).prefix(prefix).delimiter("/");
                if (continuationToken != null) {
                    reqBuilder.continuationToken(continuationToken);
                }
                ListObjectsV2Response response = client.listObjectsV2(reqBuilder.build());
                if (profilingRecorder != null) {
                    profilingRecorder.recordS3List(System.nanoTime() - startNanos);
                }
                for (S3Object obj : response.contents()) {
                    String fullKey = obj.key();
                    String blobName = fullKey.substring(path.buildAsString().length());
                    blobs.put(blobName, new BlobMetadata(blobName, obj.size()));
                }
                continuationToken = response.isTruncated() ? response.nextContinuationToken() : null;
            } while (continuationToken != null);

            return blobs;
        }

        // Write operations — not supported for read-only CLI
        @Override
        public void writeBlob(OperationPurpose purpose, String blobName, InputStream inputStream, long blobSize, boolean failIfAlreadyExists)
            throws IOException {
            throw new UnsupportedOperationException("Read-only blob container");
        }

        @Override
        public void writeMetadataBlob(
            OperationPurpose purpose,
            String blobName,
            boolean failIfAlreadyExists,
            boolean atomic,
            org.elasticsearch.core.CheckedConsumer<java.io.OutputStream, IOException> writer
        ) throws IOException {
            throw new UnsupportedOperationException("Read-only blob container");
        }

        @Override
        public void writeBlobAtomic(
            OperationPurpose purpose,
            String blobName,
            InputStream inputStream,
            long blobSize,
            boolean failIfAlreadyExists
        ) throws IOException {
            throw new UnsupportedOperationException("Read-only blob container");
        }

        @Override
        public org.elasticsearch.common.blobstore.DeleteResult delete(OperationPurpose purpose) throws IOException {
            throw new UnsupportedOperationException("Read-only blob container");
        }

        @Override
        public void deleteBlobsIgnoringIfNotExists(OperationPurpose purpose, java.util.Iterator<String> blobNames) throws IOException {
            throw new UnsupportedOperationException("Read-only blob container");
        }

        @Override
        public Map<String, BlobMetadata> listBlobs(OperationPurpose purpose) throws IOException {
            return listBlobsByPrefix(purpose, null);
        }

        @Override
        public Map<String, BlobContainer> children(OperationPurpose purpose) throws IOException {
            throw new UnsupportedOperationException("Not implemented for CLI");
        }

        @Override
        public void compareAndExchangeRegister(
            OperationPurpose purpose,
            String key,
            BytesReference expected,
            BytesReference updated,
            org.elasticsearch.action.ActionListener<org.elasticsearch.common.blobstore.OptionalBytesReference> listener
        ) {
            throw new UnsupportedOperationException("Read-only blob container");
        }

        @Override
        public void getRegister(
            OperationPurpose purpose,
            String key,
            org.elasticsearch.action.ActionListener<org.elasticsearch.common.blobstore.OptionalBytesReference> listener
        ) {
            throw new UnsupportedOperationException("Read-only blob container");
        }
    }

    private static final class ProfilingInputStream extends InputStream {
        private final InputStream delegate;
        private final ProfilingRecorder profilingRecorder;
        private final boolean ranged;
        private final long requestedBytes;
        private final long startNanos;
        private long bytesRead;
        private boolean closed;

        private ProfilingInputStream(
            InputStream delegate,
            ProfilingRecorder profilingRecorder,
            boolean ranged,
            long requestedBytes,
            long startNanos
        ) {
            this.delegate = delegate;
            this.profilingRecorder = profilingRecorder;
            this.ranged = ranged;
            this.requestedBytes = requestedBytes;
            this.startNanos = startNanos;
        }

        @Override
        public int read() throws IOException {
            int value = delegate.read();
            if (value >= 0) {
                bytesRead++;
            }
            return value;
        }

        @Override
        public int read(byte[] b, int off, int len) throws IOException {
            int count = delegate.read(b, off, len);
            if (count > 0) {
                bytesRead += count;
            }
            return count;
        }

        @Override
        public void close() throws IOException {
            if (closed) {
                return;
            }
            closed = true;
            try {
                delegate.close();
            } finally {
                profilingRecorder.recordS3Read(ranged, Math.max(requestedBytes, bytesRead), bytesRead, System.nanoTime() - startNanos);
            }
        }
    }
}
