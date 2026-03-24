package org.elasticsearch.snapshotquery;

import org.apache.lucene.document.LongPoint;
import org.apache.lucene.search.BooleanClause;
import org.apache.lucene.search.BooleanQuery;
import org.apache.lucene.search.MatchAllDocsQuery;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.Sort;
import org.apache.lucene.search.SortField;
import org.apache.lucene.search.SortedNumericSortField;
import org.elasticsearch.xcontent.XContentBuilder;
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentParserConfiguration;
import org.elasticsearch.xcontent.XContentType;

import java.io.ByteArrayOutputStream;

import java.io.IOException;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;

/**
 * Parses a full Elasticsearch search body JSON, extracting query, _source fields, and sort.
 * Supports injecting --from-date/--to-date as @timestamp range filters.
 */
public class SearchBodyParser {

    private Query query;
    private List<String> sourceFields;
    private Sort sort;

    private SearchBodyParser() {}

    public Query query() { return query; }
    public List<String> sourceFields() { return sourceFields; }
    public Sort sort() { return sort; }

    /**
     * Parse a full search body JSON string, optionally injecting date range filters.
     */
    public static SearchBodyParser parse(String json, String fromDate, String toDate) throws IOException {
        SearchBodyParser result = new SearchBodyParser();
        result.sourceFields = null;
        result.sort = null;

        String queryJson = null;

        try (XContentParser parser = XContentType.JSON.xContent().createParser(XContentParserConfiguration.EMPTY, json)) {
            parser.nextToken(); // START_OBJECT
            while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
                String field = parser.currentName();
                parser.nextToken();
                switch (field) {
                    case "query" -> queryJson = extractRawObject(parser);
                    case "_source" -> result.sourceFields = parseSourceFields(parser);
                    case "sort" -> result.sort = parseSortArray(parser);
                    default -> skipValue(parser); // track_total_hits, size, etc.
                }
            }
        }

        // Translate query
        Query baseQuery;
        if (queryJson != null) {
            baseQuery = SimpleQueryTranslator.translate(queryJson);
        } else {
            baseQuery = new MatchAllDocsQuery();
        }

        // Inject date range filter if specified
        result.query = injectDateRange(baseQuery, fromDate, toDate);

        // Default sort by @timestamp asc, doc id
        if (result.sort == null) {
            result.sort = new Sort(
                new SortedNumericSortField("@timestamp", SortField.Type.LONG),
                SortField.FIELD_DOC
            );
        }

        return result;
    }

    /**
     * Parse just the query portion (for --query / --query-file that only contain a query, not full body).
     */
    public static SearchBodyParser fromQueryOnly(String queryJson, String fromDate, String toDate) throws IOException {
        SearchBodyParser result = new SearchBodyParser();
        result.sourceFields = null;

        Query baseQuery = SimpleQueryTranslator.translate(queryJson);
        result.query = injectDateRange(baseQuery, fromDate, toDate);
        result.sort = new Sort(
            new SortedNumericSortField("@timestamp", SortField.Type.LONG),
            SortField.FIELD_DOC
        );

        return result;
    }

    private static Query injectDateRange(Query baseQuery, String fromDate, String toDate) {
        if (fromDate == null && toDate == null) {
            return baseQuery;
        }

        long fromMillis = fromDate != null ? parseDateToMillis(fromDate) : Long.MIN_VALUE;
        long toMillis = toDate != null ? parseDateToMillis(toDate) : Long.MAX_VALUE;

        // toDate is exclusive (matches elastic-utils behavior)
        if (toDate != null) {
            toMillis = toMillis - 1;
        }

        Query rangeQuery = LongPoint.newRangeQuery("@timestamp", fromMillis, toMillis);

        BooleanQuery.Builder bqb = new BooleanQuery.Builder();
        bqb.add(baseQuery, BooleanClause.Occur.FILTER);
        bqb.add(rangeQuery, BooleanClause.Occur.FILTER);
        return bqb.build();
    }

    static long parseDateToMillis(String date) {
        try {
            // Try ISO instant first (2025-01-15T10:00:00Z)
            return Instant.parse(date).toEpochMilli();
        } catch (DateTimeParseException e) {
            // Try date only (2025-01-15) → start of day UTC
            return LocalDate.parse(date, DateTimeFormatter.ISO_LOCAL_DATE)
                .atStartOfDay(ZoneOffset.UTC)
                .toInstant()
                .toEpochMilli();
        }
    }

    private static List<String> parseSourceFields(XContentParser parser) throws IOException {
        if (parser.currentToken() == XContentParser.Token.START_ARRAY) {
            List<String> fields = new ArrayList<>();
            while (parser.nextToken() != XContentParser.Token.END_ARRAY) {
                fields.add(parser.text());
            }
            return fields;
        } else if (parser.currentToken() == XContentParser.Token.VALUE_BOOLEAN) {
            // _source: false means no source
            return parser.booleanValue() ? null : List.of();
        }
        skipValue(parser);
        return null;
    }

    private static Sort parseSortArray(XContentParser parser) throws IOException {
        if (parser.currentToken() != XContentParser.Token.START_ARRAY) {
            skipValue(parser);
            return null;
        }

        List<SortField> sortFields = new ArrayList<>();
        while (parser.nextToken() != XContentParser.Token.END_ARRAY) {
            if (parser.currentToken() == XContentParser.Token.START_OBJECT) {
                parser.nextToken(); // field name
                String fieldName = parser.currentName();
                parser.nextToken();

                boolean reverse = false;
                if (parser.currentToken() == XContentParser.Token.VALUE_STRING) {
                    reverse = "desc".equalsIgnoreCase(parser.text());
                } else if (parser.currentToken() == XContentParser.Token.START_OBJECT) {
                    while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
                        if ("order".equals(parser.currentName())) {
                            parser.nextToken();
                            reverse = "desc".equalsIgnoreCase(parser.text());
                        } else {
                            parser.nextToken();
                            skipValue(parser);
                        }
                    }
                }
                parser.nextToken(); // END_OBJECT

                if ("@timestamp".equals(fieldName)) {
                    sortFields.add(new SortedNumericSortField("@timestamp", SortField.Type.LONG, reverse));
                } else if ("_doc".equals(fieldName)) {
                    sortFields.add(reverse ? new SortField(null, SortField.Type.DOC, true) : SortField.FIELD_DOC);
                } else {
                    // Generic long sort for unknown fields
                    sortFields.add(new SortedNumericSortField(fieldName, SortField.Type.LONG, reverse));
                }
            } else if (parser.currentToken() == XContentParser.Token.VALUE_STRING) {
                // "_doc" shorthand
                String val = parser.text();
                if ("_doc".equals(val)) {
                    sortFields.add(SortField.FIELD_DOC);
                }
            }
        }

        if (sortFields.isEmpty()) {
            return null;
        }
        // Always append FIELD_DOC for tiebreaking if not already present
        boolean hasDoc = sortFields.stream().anyMatch(sf -> sf.getType() == SortField.Type.DOC);
        if (!hasDoc) {
            sortFields.add(SortField.FIELD_DOC);
        }
        return new Sort(sortFields.toArray(new SortField[0]));
    }

    /**
     * Extract a raw JSON object as a string (for passing to SimpleQueryTranslator).
     */
    private static String extractRawObject(XContentParser parser) throws IOException {
        if (parser.currentToken() != XContentParser.Token.START_OBJECT) {
            skipValue(parser);
            return null;
        }
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (XContentBuilder builder = new XContentBuilder(XContentType.JSON.xContent(), baos)) {
            builder.copyCurrentStructure(parser);
        }
        return baos.toString(java.nio.charset.StandardCharsets.UTF_8);
    }

    private static void skipValue(XContentParser parser) throws IOException {
        XContentParser.Token token = parser.currentToken();
        if (token == XContentParser.Token.START_OBJECT) {
            int depth = 1;
            while (depth > 0) {
                token = parser.nextToken();
                if (token == XContentParser.Token.START_OBJECT) depth++;
                else if (token == XContentParser.Token.END_OBJECT) depth--;
            }
        } else if (token == XContentParser.Token.START_ARRAY) {
            int depth = 1;
            while (depth > 0) {
                token = parser.nextToken();
                if (token == XContentParser.Token.START_ARRAY) depth++;
                else if (token == XContentParser.Token.END_ARRAY) depth--;
            }
        }
        // scalar values are already consumed
    }
}
