package org.elasticsearch.snapshotquery;

import org.apache.lucene.store.BaseDirectory;
import org.apache.lucene.store.BufferedIndexInput;
import org.apache.lucene.store.IOContext;
import org.apache.lucene.store.IndexInput;
import org.apache.lucene.store.IndexOutput;
import org.apache.lucene.store.NoLockFactory;
import org.elasticsearch.common.blobstore.BlobContainer;
import org.elasticsearch.common.blobstore.OperationPurpose;
import org.elasticsearch.index.snapshots.blobstore.BlobStoreIndexShardSnapshot;
import org.elasticsearch.index.snapshots.blobstore.BlobStoreIndexShardSnapshot.FileInfo;

import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.ByteBuffer;
import java.util.Collection;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;

/**
 * Read-only Lucene Directory backed by S3 via a BlobContainer.
 * Maps Lucene file names to blob names/parts using BlobStoreIndexShardSnapshot metadata.
 */
public class SnapshotQueryDirectory extends BaseDirectory {

    private final BlobContainer blobContainer;
    private final Map<String, FileInfo> fileInfoMap;
    private final ProfilingRecorder profilingRecorder;
    private final String indexName;
    private final int shardId;

    public SnapshotQueryDirectory(BlobContainer blobContainer, BlobStoreIndexShardSnapshot shardSnapshot) {
        this(blobContainer, shardSnapshot, null, null, -1);
    }

    public SnapshotQueryDirectory(
        BlobContainer blobContainer,
        BlobStoreIndexShardSnapshot shardSnapshot,
        ProfilingRecorder profilingRecorder,
        String indexName,
        int shardId
    ) {
        super(NoLockFactory.INSTANCE);
        this.blobContainer = blobContainer;
        this.profilingRecorder = profilingRecorder;
        this.indexName = indexName;
        this.shardId = shardId;
        this.fileInfoMap = new HashMap<>();
        for (FileInfo fileInfo : shardSnapshot.indexFiles()) {
            fileInfoMap.put(fileInfo.physicalName(), fileInfo);
        }
    }

    @Override
    public String[] listAll() {
        return fileInfoMap.keySet().toArray(new String[0]);
    }

    @Override
    public long fileLength(String name) throws IOException {
        FileInfo fileInfo = fileInfoMap.get(name);
        if (fileInfo == null) {
            throw new java.io.FileNotFoundException("File [" + name + "] not found in snapshot");
        }
        return fileInfo.length();
    }

    @Override
    public IndexInput openInput(String name, IOContext context) throws IOException {
        FileInfo fileInfo = fileInfoMap.get(name);
        if (fileInfo == null) {
            throw new java.io.FileNotFoundException("File [" + name + "] not found in snapshot");
        }

        // Small files: content may be embedded in the metadata hash
        if (fileInfo.metadata().hash() != null && fileInfo.metadata().hash().length > 0 && fileInfo.metadata().hashEqualsContents()) {
            byte[] data = new byte[fileInfo.metadata().hash().length];
            System.arraycopy(
                fileInfo.metadata().hash().bytes,
                fileInfo.metadata().hash().offset,
                data,
                0,
                fileInfo.metadata().hash().length
            );
            return new ByteArrayIndexInput(name, data);
        }

        return new S3IndexInput(name, blobContainer, fileInfo, profilingRecorder, indexName, shardId, 0, fileInfo.length(), BufferedIndexInput.BUFFER_SIZE);
    }

    @Override
    public void close() {
        // nothing to close
    }

    // Read-only directory — write operations are unsupported
    @Override
    public IndexOutput createOutput(String name, IOContext context) {
        throw new UnsupportedOperationException("Read-only directory");
    }

    @Override
    public IndexOutput createTempOutput(String prefix, String suffix, IOContext context) {
        throw new UnsupportedOperationException("Read-only directory");
    }

    @Override
    public void sync(Collection<String> names) {
        // no-op for read-only
    }

    @Override
    public void syncMetaData() {
        // no-op
    }

    @Override
    public void rename(String source, String dest) {
        throw new UnsupportedOperationException("Read-only directory");
    }

    @Override
    public void deleteFile(String name) {
        throw new UnsupportedOperationException("Read-only directory");
    }

    // obtainLock is final in BaseDirectory and uses NoLockFactory passed to constructor

    @Override
    public Set<String> getPendingDeletions() {
        return Set.of();
    }

    /**
     * IndexInput that reads from an S3 BlobContainer, handling multi-part files.
     */
    static class S3IndexInput extends BufferedIndexInput {
        private static final int DEFAULT_READ_AHEAD_BYTES = 1024 * 1024;
        private static final int STORED_FIELDS_READ_AHEAD_BYTES = 4 * 1024 * 1024;
        private static final int DOC_VALUES_READ_AHEAD_BYTES = 2 * 1024 * 1024;
        private static final long DEFAULT_FULL_CACHE_BYTES = 4L * 1024 * 1024;
        private static final long LARGE_METADATA_FULL_CACHE_BYTES = 16L * 1024 * 1024;

        private final BlobContainer blobContainer;
        private final FileInfo fileInfo;
        private final long offset;
        private final long length;
        private final ProfilingRecorder profilingRecorder;
        private final String indexName;
        private final int shardId;
        private byte[] fullSliceCache;
        private byte[] readaheadCache;
        private long readaheadStart = -1;
        private int readaheadLength;

        S3IndexInput(String name, BlobContainer blobContainer, FileInfo fileInfo) {
            this(name, blobContainer, fileInfo, null, null, -1, 0, fileInfo.length(), BufferedIndexInput.BUFFER_SIZE);
        }

        private S3IndexInput(
            String name,
            BlobContainer blobContainer,
            FileInfo fileInfo,
            ProfilingRecorder profilingRecorder,
            String indexName,
            int shardId,
            long offset,
            long length,
            int bufferSize
        ) {
            super("S3IndexInput(" + name + ")", bufferSize);
            this.blobContainer = blobContainer;
            this.fileInfo = fileInfo;
            this.profilingRecorder = profilingRecorder;
            this.indexName = indexName;
            this.shardId = shardId;
            this.offset = offset;
            this.length = length;
        }

        @Override
        protected void readInternal(ByteBuffer b) throws IOException {
            long pos = offset + getFilePointer();
            int remaining = b.remaining();

            if (shouldFullSliceCache()) {
                ensureFullSliceCache();
                int cacheOffset = Math.toIntExact(pos - offset);
                b.put(fullSliceCache, cacheOffset, remaining);
                return;
            }

            while (remaining > 0) {
                if (readaheadContains(pos)) {
                    int cacheOffset = Math.toIntExact(pos - readaheadStart);
                    int fromCache = Math.min(remaining, readaheadLength - cacheOffset);
                    b.put(readaheadCache, cacheOffset, fromCache);
                    remaining -= fromCache;
                    pos += fromCache;
                    continue;
                }

                int toFetch = Math.toIntExact(Math.min(length - (pos - offset), Math.max(remaining, readAheadBytes())));
                readaheadCache = fetchBytes(pos, toFetch);
                readaheadStart = pos;
                readaheadLength = readaheadCache.length;
            }
        }

        @Override
        protected void seekInternal(long pos) {
            // BufferedIndexInput handles seeking; we just need to validate
            if (pos < 0 || pos > length) {
                throw new IllegalArgumentException("Seek position " + pos + " out of range [0, " + length + "]");
            }
        }

        @Override
        public long length() {
            return length;
        }

        @Override
        public IndexInput slice(String sliceDescription, long sliceOffset, long sliceLength) {
            if (sliceOffset < 0 || sliceLength < 0 || sliceOffset + sliceLength > length) {
                throw new IllegalArgumentException(
                    "slice(offset=" + sliceOffset + ", length=" + sliceLength + ") out of bounds for length=" + length
                );
            }
            return new S3IndexInput(
                sliceDescription,
                blobContainer,
                fileInfo,
                profilingRecorder,
                indexName,
                shardId,
                offset + sliceOffset,
                sliceLength,
                BUFFER_SIZE
            );
        }

        @Override
        public S3IndexInput clone() {
            return (S3IndexInput) super.clone();
        }

        @Override
        public void close() {
            // nothing to close; each read opens/closes its own stream
        }

        private boolean readaheadContains(long pos) {
            return readaheadCache != null && pos >= readaheadStart && pos < readaheadStart + readaheadLength;
        }

        private void ensureFullSliceCache() throws IOException {
            if (fullSliceCache == null) {
                fullSliceCache = fetchBytes(offset, Math.toIntExact(length));
            }
        }

        private boolean shouldFullSliceCache() {
            String fileName = fileInfo.physicalName();
            if (length <= DEFAULT_FULL_CACHE_BYTES) {
                return true;
            }
            return length <= LARGE_METADATA_FULL_CACHE_BYTES && (
                fileName.endsWith(".fdx")
                    || fileName.endsWith(".dvm")
                    || fileName.endsWith(".kdd")
                    || fileName.endsWith(".kdi")
                    || fileName.endsWith(".doc")
                    || fileName.endsWith(".tim")
                    || fileName.endsWith(".tip")
            );
        }

        private int readAheadBytes() {
            String fileName = fileInfo.physicalName();
            if (fileName.endsWith(".fdt")) {
                return STORED_FIELDS_READ_AHEAD_BYTES;
            }
            if (fileName.endsWith(".dvd")) {
                return DOC_VALUES_READ_AHEAD_BYTES;
            }
            return DEFAULT_READ_AHEAD_BYTES;
        }

        private byte[] fetchBytes(long absolutePosition, int len) throws IOException {
            byte[] result = new byte[len];
            int written = 0;
            long pos = absolutePosition;
            long readStartNanos = System.nanoTime();

            while (written < len) {
                int part = fileInfo.numberOfParts() == 1 ? 0 : (int) (pos / fileInfo.partBytes(0));
                if (part >= fileInfo.numberOfParts()) {
                    throw new EOFException("Read past end of file: position=" + pos + " length=" + fileInfo.length());
                }

                long partStart = (long) part * fileInfo.partBytes(0);
                long posInPart = pos - partStart;
                long partLen = fileInfo.partBytes(part);
                int toRead = (int) Math.min(len - written, partLen - posInPart);

                String blobName = fileInfo.partName(part);
                try (InputStream is = blobContainer.readBlob(OperationPurpose.SNAPSHOT_DATA, blobName, posInPart, toRead)) {
                    int partRead = is.readNBytes(result, written, toRead);
                    written += partRead;
                    pos += partRead;
                    if (partRead < toRead) {
                        throw new EOFException("Short read from S3: expected " + toRead + " bytes but got " + partRead);
                    }
                }
            }

            if (profilingRecorder != null && indexName != null) {
                profilingRecorder.recordLuceneFileRead(indexName, shardId, fileInfo.physicalName(), written, System.nanoTime() - readStartNanos);
            }
            return result;
        }
    }

    /**
     * Simple IndexInput backed by a byte array, for small files whose content is embedded in metadata.
     */
    static class ByteArrayIndexInput extends IndexInput {
        private final byte[] data;
        private int pos;

        ByteArrayIndexInput(String name, byte[] data) {
            super("ByteArrayIndexInput(" + name + ")");
            this.data = data;
            this.pos = 0;
        }

        @Override
        public byte readByte() throws IOException {
            if (pos >= data.length) throw new EOFException();
            return data[pos++];
        }

        @Override
        public void readBytes(byte[] b, int offset, int len) throws IOException {
            if (pos + len > data.length) throw new EOFException();
            System.arraycopy(data, pos, b, offset, len);
            pos += len;
        }

        @Override
        public long length() {
            return data.length;
        }

        @Override
        public long getFilePointer() {
            return pos;
        }

        @Override
        public void seek(long newPos) throws IOException {
            if (newPos < 0 || newPos > data.length) {
                throw new EOFException("Seek to " + newPos + " out of range [0, " + data.length + "]");
            }
            pos = (int) newPos;
        }

        @Override
        public IndexInput slice(String sliceDescription, long sliceOffset, long sliceLength) throws IOException {
            if (sliceOffset < 0 || sliceLength < 0 || sliceOffset + sliceLength > data.length) {
                throw new IllegalArgumentException("Invalid slice");
            }
            byte[] sliceData = new byte[(int) sliceLength];
            System.arraycopy(data, (int) sliceOffset, sliceData, 0, (int) sliceLength);
            return new ByteArrayIndexInput(sliceDescription, sliceData);
        }

        @Override
        public void close() {
            // no-op
        }
    }
}
