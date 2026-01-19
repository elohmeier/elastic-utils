/**
 * Represents a field in the schema, including nested fields for STRUCT types
 */
export interface NestedField {
  name: string;
  type: string;
  path: string; // Full path for SQL queries, e.g., "_source"."@timestamp"
  displayPath: string; // Display path, e.g., _source.@timestamp
  nullable: boolean;
  children?: NestedField[];
}

/**
 * Parse a DuckDB STRUCT type definition into nested fields
 * Example input: STRUCT(host STRUCT("name" VARCHAR), message VARCHAR, "@timestamp" TIMESTAMP)
 */
export function parseStructType(
  typeDef: string,
  parentPath: string = "",
  parentDisplayPath: string = "",
): NestedField[] {
  const fields: NestedField[] = [];

  // Check if this is a STRUCT type
  if (!typeDef.toUpperCase().startsWith("STRUCT(")) {
    return fields;
  }

  // Extract the content between STRUCT( and the matching )
  const content = extractStructContent(typeDef);
  if (!content) return fields;

  // Parse individual field definitions
  const fieldDefs = splitStructFields(content);

  for (const fieldDef of fieldDefs) {
    const parsed = parseFieldDef(fieldDef.trim(), parentPath, parentDisplayPath);
    if (parsed) {
      fields.push(parsed);
    }
  }

  return fields;
}

/**
 * Extract content between STRUCT( and matching )
 */
function extractStructContent(typeDef: string): string | null {
  const start = typeDef.indexOf("(");
  if (start === -1) return null;

  let depth = 0;
  let end = -1;

  for (let i = start; i < typeDef.length; i++) {
    if (typeDef[i] === "(") depth++;
    if (typeDef[i] === ")") depth--;
    if (depth === 0) {
      end = i;
      break;
    }
  }

  if (end === -1) return null;
  return typeDef.slice(start + 1, end);
}

/**
 * Split struct fields by comma, respecting nested parentheses
 */
function splitStructFields(content: string): string[] {
  const fields: string[] = [];
  let current = "";
  let depth = 0;

  for (let i = 0; i < content.length; i++) {
    const char = content[i];

    if (char === "(") depth++;
    if (char === ")") depth--;

    if (char === "," && depth === 0) {
      if (current.trim()) {
        fields.push(current.trim());
      }
      current = "";
    } else {
      current += char;
    }
  }

  if (current.trim()) {
    fields.push(current.trim());
  }

  return fields;
}

/**
 * Parse a single field definition like: "fieldName" VARCHAR or fieldName STRUCT(...)
 */
function parseFieldDef(
  fieldDef: string,
  parentPath: string,
  parentDisplayPath: string,
): NestedField | null {
  // Handle quoted field names like "@timestamp" TIMESTAMP
  let fieldName: string;
  let typeStart: number;

  if (fieldDef.startsWith("\"")) {
    // Quoted field name
    const endQuote = fieldDef.indexOf("\"", 1);
    if (endQuote === -1) return null;
    fieldName = fieldDef.slice(1, endQuote);
    typeStart = endQuote + 1;
  } else {
    // Unquoted field name - find first space
    const spaceIdx = fieldDef.indexOf(" ");
    if (spaceIdx === -1) return null;
    fieldName = fieldDef.slice(0, spaceIdx);
    typeStart = spaceIdx;
  }

  const typeDef = fieldDef.slice(typeStart).trim();
  if (!typeDef) return null;

  // Build paths
  const needsQuotes = fieldName.includes("@") || fieldName.includes("-") || fieldName.includes(".");
  const quotedName = needsQuotes ? `"${fieldName}"` : `"${fieldName}"`;
  const path = parentPath ? `${parentPath}.${quotedName}` : quotedName;
  const displayPath = parentDisplayPath ? `${parentDisplayPath}.${fieldName}` : fieldName;

  const field: NestedField = {
    name: fieldName,
    type: typeDef,
    path,
    displayPath,
    nullable: true,
  };

  // If this is a STRUCT, parse children recursively
  if (typeDef.toUpperCase().startsWith("STRUCT(")) {
    field.children = parseStructType(typeDef, path, displayPath);
  }

  return field;
}

/**
 * Build a nested field tree from the schema
 */
export function buildFieldTree(
  schema: Array<{ name: string; type: string; nullable: boolean }>,
): NestedField[] {
  return schema.map((field) => {
    const needsQuotes = field.name.includes("@") || field.name.includes("-") || field.name.includes(".");
    const quotedName = `"${field.name}"`;
    const path = quotedName;

    const nestedField: NestedField = {
      name: field.name,
      type: field.type,
      path,
      displayPath: field.name,
      nullable: field.nullable,
    };

    // If this is a STRUCT, parse children
    if (field.type.toUpperCase().startsWith("STRUCT(")) {
      nestedField.children = parseStructType(field.type, path, field.name);
    }

    return nestedField;
  });
}

/**
 * Flatten a field tree to get all leaf paths (for search)
 */
export function flattenFieldTree(fields: NestedField[], includeStructs = false): NestedField[] {
  const result: NestedField[] = [];

  for (const field of fields) {
    if (field.children && field.children.length > 0) {
      if (includeStructs) {
        result.push(field);
      }
      result.push(...flattenFieldTree(field.children, includeStructs));
    } else {
      result.push(field);
    }
  }

  return result;
}

/**
 * Get a simplified base type (without STRUCT contents)
 */
export function getBaseType(type: string): string {
  const upper = type.toUpperCase();
  if (upper.startsWith("STRUCT")) return "STRUCT";
  if (upper.startsWith("LIST") || upper.includes("[]")) return "LIST";
  if (upper.startsWith("MAP")) return "MAP";
  return type;
}
