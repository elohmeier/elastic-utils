package org.elasticsearch.snapshotquery;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import org.apache.lucene.document.LongPoint;
import org.apache.lucene.index.Term;
import org.apache.lucene.search.BooleanClause;
import org.apache.lucene.search.BooleanQuery;
import org.apache.lucene.search.FieldExistsQuery;
import org.apache.lucene.search.MatchAllDocsQuery;
import org.apache.lucene.search.PrefixQuery;
import org.apache.lucene.search.Query;
import org.apache.lucene.search.TermQuery;
import org.apache.lucene.search.TermRangeQuery;
import org.apache.lucene.search.WildcardQuery;
import org.elasticsearch.xcontent.XContentParser;
import org.elasticsearch.xcontent.XContentParserConfiguration;
import org.elasticsearch.xcontent.XContentType;

/**
 * Translates a subset of Elasticsearch Query DSL JSON into Lucene Query objects. Supports:
 * match_all, term, terms, bool, range, wildcard, prefix, exists.
 */
public class SimpleQueryTranslator {

  public static Query translate(String queryJson) throws IOException {
    try (XContentParser parser =
        XContentType.JSON.xContent().createParser(XContentParserConfiguration.EMPTY, queryJson)) {
      parser.nextToken(); // START_OBJECT
      return parseQuery(parser);
    }
  }

  private static Query parseQuery(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    parser.nextToken(); // field name
    if (parser.currentToken() == XContentParser.Token.END_OBJECT) {
      return new MatchAllDocsQuery();
    }
    String queryType = parser.currentName();
    parser.nextToken(); // value or start_object
    Query query =
        switch (queryType) {
          case "match_all" -> parseMatchAll(parser);
          case "term" -> parseTerm(parser);
          case "terms" -> parseTerms(parser);
          case "bool" -> parseBool(parser);
          case "range" -> parseRange(parser);
          case "wildcard" -> parseWildcard(parser);
          case "prefix" -> parsePrefix(parser);
          case "exists" -> parseExists(parser);
          default -> throw new IllegalArgumentException("Unsupported query type: " + queryType);
        };
    parser.nextToken(); // END_OBJECT (outer)
    return query;
  }

  private static Query parseMatchAll(XContentParser parser) throws IOException {
    skipObject(parser);
    return new MatchAllDocsQuery();
  }

  private static Query parseTerm(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    parser.nextToken();
    String field = parser.currentName();
    parser.nextToken();

    String value;
    if (parser.currentToken() == XContentParser.Token.START_OBJECT) {
      // { "field": { "value": "..." } } form
      value = null;
      while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
        if ("value".equals(parser.currentName())) {
          parser.nextToken();
          value = parser.text();
        } else {
          parser.nextToken();
          // skip boost, etc.
        }
      }
      if (value == null) {
        throw new IllegalArgumentException("term query missing 'value' field");
      }
    } else {
      // { "field": "value" } shorthand
      value = parser.text();
    }

    parser.nextToken(); // END_OBJECT
    return new TermQuery(new Term(field, value));
  }

  private static Query parseTerms(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    parser.nextToken();
    String field = parser.currentName();
    parser.nextToken();
    ensureToken(XContentParser.Token.START_ARRAY, parser.currentToken());

    BooleanQuery.Builder bqb = new BooleanQuery.Builder();
    while (parser.nextToken() != XContentParser.Token.END_ARRAY) {
      bqb.add(new TermQuery(new Term(field, parser.text())), BooleanClause.Occur.SHOULD);
    }

    parser.nextToken(); // END_OBJECT
    return bqb.build();
  }

  private static Query parseBool(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    BooleanQuery.Builder bqb = new BooleanQuery.Builder();

    while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
      String clauseType = parser.currentName();
      parser.nextToken();
      BooleanClause.Occur occur =
          switch (clauseType) {
            case "must" -> BooleanClause.Occur.MUST;
            case "filter" -> BooleanClause.Occur.FILTER;
            case "should" -> BooleanClause.Occur.SHOULD;
            case "must_not" -> BooleanClause.Occur.MUST_NOT;
            case "minimum_should_match" -> {
              // skip this parameter
              yield null;
            }
            default -> throw new IllegalArgumentException("Unknown bool clause: " + clauseType);
          };

      if (occur == null) {
        continue; // skipped parameter
      }

      if (parser.currentToken() == XContentParser.Token.START_ARRAY) {
        while (parser.nextToken() != XContentParser.Token.END_ARRAY) {
          bqb.add(parseQuery(parser), occur);
        }
      } else {
        bqb.add(parseQuery(parser), occur);
      }
    }

    return bqb.build();
  }

  private static Query parseRange(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    parser.nextToken();
    String field = parser.currentName();
    parser.nextToken();
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());

    String gte = null, gt = null, lte = null, lt = null;
    boolean isNumeric = false;

    List<String[]> params = new ArrayList<>();
    while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
      String param = parser.currentName();
      parser.nextToken();
      String val = parser.text();
      params.add(new String[] {param, val});
      try {
        Long.parseLong(val);
        isNumeric = true;
      } catch (NumberFormatException e) {
        // not numeric
      }
    }

    for (String[] p : params) {
      switch (p[0]) {
        case "gte" -> gte = p[1];
        case "gt" -> gt = p[1];
        case "lte" -> lte = p[1];
        case "lt" -> lt = p[1];
          // skip format, time_zone, etc.
      }
    }

    parser.nextToken(); // END_OBJECT

    if (isNumeric) {
      long lower = Long.MIN_VALUE;
      long upper = Long.MAX_VALUE;
      if (gte != null) lower = Long.parseLong(gte);
      if (gt != null) lower = Long.parseLong(gt) + 1;
      if (lte != null) upper = Long.parseLong(lte);
      if (lt != null) upper = Long.parseLong(lt) - 1;
      return LongPoint.newRangeQuery(field, lower, upper);
    } else {
      String lowerTerm = gte != null ? gte : gt;
      String upperTerm = lte != null ? lte : lt;
      boolean includeLower = gte != null;
      boolean includeUpper = lte != null;
      return new TermRangeQuery(
          field,
          lowerTerm != null ? new org.apache.lucene.util.BytesRef(lowerTerm) : null,
          upperTerm != null ? new org.apache.lucene.util.BytesRef(upperTerm) : null,
          includeLower,
          includeUpper);
    }
  }

  private static Query parseWildcard(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    parser.nextToken();
    String field = parser.currentName();
    parser.nextToken();

    String value;
    if (parser.currentToken() == XContentParser.Token.START_OBJECT) {
      value = null;
      while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
        if ("value".equals(parser.currentName())) {
          parser.nextToken();
          value = parser.text();
        } else {
          parser.nextToken();
        }
      }
      if (value == null) {
        throw new IllegalArgumentException("wildcard query missing 'value' field");
      }
    } else {
      value = parser.text();
    }

    parser.nextToken(); // END_OBJECT
    return new WildcardQuery(new Term(field, value));
  }

  private static Query parsePrefix(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    parser.nextToken();
    String field = parser.currentName();
    parser.nextToken();

    String value;
    if (parser.currentToken() == XContentParser.Token.START_OBJECT) {
      value = null;
      while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
        if ("value".equals(parser.currentName())) {
          parser.nextToken();
          value = parser.text();
        } else {
          parser.nextToken();
        }
      }
      if (value == null) {
        throw new IllegalArgumentException("prefix query missing 'value' field");
      }
    } else {
      value = parser.text();
    }

    parser.nextToken(); // END_OBJECT
    return new PrefixQuery(new Term(field, value));
  }

  private static Query parseExists(XContentParser parser) throws IOException {
    ensureToken(XContentParser.Token.START_OBJECT, parser.currentToken());
    String field = null;
    while (parser.nextToken() != XContentParser.Token.END_OBJECT) {
      if ("field".equals(parser.currentName())) {
        parser.nextToken();
        field = parser.text();
      } else {
        parser.nextToken();
      }
    }
    if (field == null) {
      throw new IllegalArgumentException("exists query missing 'field'");
    }
    return new FieldExistsQuery(field);
  }

  private static void skipObject(XContentParser parser) throws IOException {
    if (parser.currentToken() == XContentParser.Token.START_OBJECT) {
      int depth = 1;
      while (depth > 0) {
        XContentParser.Token token = parser.nextToken();
        if (token == XContentParser.Token.START_OBJECT) depth++;
        else if (token == XContentParser.Token.END_OBJECT) depth--;
      }
    }
  }

  private static void ensureToken(XContentParser.Token expected, XContentParser.Token actual) {
    if (expected != actual) {
      throw new IllegalArgumentException("Expected token " + expected + " but got " + actual);
    }
  }
}
