import { defineRule } from "@oxlint/plugins";

import type { ESTree, Scope, SourceCode, Variable } from "@oxlint/plugins";

type LiteralValue = ESTree.Literal["value"];

type MemberCallee = Extract<
  ESTree.Expression,
  { computed: boolean; object: ESTree.Expression; property: ESTree.Node }
>;

const moduleMockMethods = new Set<LiteralValue>(["doMock", "mock", "unstable_mockModule"]);
const frameworkGlobalNames = new Set(["vi", "jest"]);

const frameworkImports = new Map([
  ["vitest", "vi"],
  ["@jest/globals", "jest"],
]);

function resolveVariable(
  sourceCode: SourceCode,
  identifier: ESTree.IdentifierReference,
): Variable | null {
  let scope: Scope | null = sourceCode.getScope(identifier);

  while (scope !== null) {
    const variable = scope.set.get(identifier.name);

    if (variable !== undefined) return variable;

    scope = scope.upper;
  }

  return null;
}

function importedName(node: ESTree.Node): string | null {
  if (node.type !== "ImportSpecifier") return null;

  return node.imported.type === "Identifier" ? node.imported.name : node.imported.value;
}

function isFrameworkGlobal(sourceCode: SourceCode, identifier: ESTree.IdentifierReference): boolean {
  return frameworkGlobalNames.has(identifier.name) && sourceCode.isGlobalReference(identifier);
}

function isFrameworkImport(definition: Variable["defs"][number]): boolean {
  const declaration = definition.parent;

  if (definition.type !== "ImportBinding" || declaration?.type !== "ImportDeclaration") {
    return false;
  }

  return frameworkImports.get(declaration.source.value) === importedName(definition.node);
}

function isFrameworkIdentifier(
  sourceCode: SourceCode,
  identifier: ESTree.IdentifierReference,
): boolean {
  if (isFrameworkGlobal(sourceCode, identifier)) return true;

  const variable = resolveVariable(sourceCode, identifier);

  if (variable === null || variable.defs.length === 0) {
    return frameworkGlobalNames.has(identifier.name);
  }

  return variable.defs.some(isFrameworkImport);
}

function isTestFrameworkObject(
  sourceCode: SourceCode,
  expression: ESTree.Expression,
): expression is ESTree.IdentifierReference {
  return expression.type === "Identifier" && isFrameworkIdentifier(sourceCode, expression);
}

function isMemberCallee(callee: ESTree.Expression): callee is MemberCallee {
  return "property" in callee && "object" in callee && "computed" in callee;
}

function memberMethodName(callee: MemberCallee): LiteralValue {
  const property = callee.property;

  if (callee.computed) return property.type === "Literal" ? property.value : null;

  return property.type === "Identifier" ? property.name : null;
}

function moduleMockCall(sourceCode: SourceCode, callee: ESTree.Expression): boolean {
  return (
    isMemberCallee(callee) &&
    isTestFrameworkObject(sourceCode, callee.object) &&
    moduleMockMethods.has(memberMethodName(callee))
  );
}

/** Ban test framework module mocking in favor of real dependency seams. */
export const noModuleMockingRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallow Vitest and Jest module mocking; tests must replace dependencies through real interfaces.",
    },
    messages: {
      moduleMock:
        "Replace module mocking with dependency injection through a real interface, service layer, or faithful test implementation.",
    },
  },
  createOnce(context) {
    return {
      CallExpression(node) {
        if (node.callee.type === "Super" || node.callee.type === "V8IntrinsicExpression") return;

        if (moduleMockCall(context.sourceCode, node.callee)) {
          context.report({ node, messageId: "moduleMock" });
        }
      },
    };
  },
});
