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
        @Nullable String resolve
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

        if (trustAllCerts || resolve != null) {
            ApacheHttpClient.Builder httpBuilder = ApacheHttpClient.builder();

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
        }

        S3Client client = builder.build();
        BlobPath rootPath = basePath.isEmpty() ? BlobPath.EMPTY : BlobPath.EMPTY.add(basePath);
        return new S3Access(client, bucket, rootPath);
    }

    public static class S3Access implements Closeable {
        private final S3Client client;
        private final String bucket;
        private final BlobPath rootPath;

        S3Access(S3Client client, String bucket, BlobPath rootPath) {
            this.client = client;
            this.bucket = bucket;
            this.rootPath = rootPath;
        }

        public BlobContainer rootContainer() {
            return containerFor(rootPath);
        }

        public BlobContainer containerFor(BlobPath path) {
            return new SimpleS3BlobContainer(client, bucket, path);
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

        SimpleS3BlobContainer(S3Client client, String bucket, BlobPath path) {
            this.client = client;
            this.bucket = bucket;
            this.path = path;
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
            try {
                client.headObject(b -> b.bucket(bucket).key(blobKey(blobName)));
                return true;
            } catch (NoSuchKeyException e) {
                return false;
            }
        }

        @Override
        public InputStream readBlob(OperationPurpose purpose, String blobName) throws IOException {
            try {
                GetObjectRequest request = GetObjectRequest.builder().bucket(bucket).key(blobKey(blobName)).build();
                return client.getObject(request);
            } catch (NoSuchKeyException e) {
                throw new NoSuchFileException("Blob [" + blobKey(blobName) + "] not found in bucket [" + bucket + "]");
            }
        }

        @Override
        public InputStream readBlob(OperationPurpose purpose, String blobName, long position, long length) throws IOException {
            try {
                String range = "bytes=" + position + "-" + (position + length - 1);
                GetObjectRequest request = GetObjectRequest.builder().bucket(bucket).key(blobKey(blobName)).range(range).build();
                return client.getObject(request);
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
                ListObjectsV2Request.Builder reqBuilder = ListObjectsV2Request.builder().bucket(bucket).prefix(prefix).delimiter("/");
                if (continuationToken != null) {
                    reqBuilder.continuationToken(continuationToken);
                }
                ListObjectsV2Response response = client.listObjectsV2(reqBuilder.build());
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
}
