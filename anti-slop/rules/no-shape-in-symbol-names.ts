import { defineRule } from "@oxlint/plugins";
import type { ESTree } from "@oxlint/plugins";

const FORBIDDEN_SYMBOL_NAME = "shape";

function containsForbiddenSymbolName(name: string): boolean {
  return name.toLowerCase().includes(FORBIDDEN_SYMBOL_NAME);
}

/** Ban vague symbol names without rejecting property names owned by another API. */
export const noForbiddenTermInSymbolNamesRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        'Disallow the case-insensitive substring "shape" in locally named JavaScript, TypeScript, private, and JSX symbols.',
    },
    messages: {
      forbiddenSymbolName:
        'Rename symbol "{{name}}" for its domain role; "shape" describes structure rather than ownership.',
    },
  },
  createOnce(context) {
    const reportForbiddenSymbolName = (node: ESTree.Node & { name: string }) => {
      if (!containsForbiddenSymbolName(node.name)) return;
      // Property access does not declare a name: libraries such as Zod own it.
      // Object, class, and interface property declarations remain checked.
      if (
        node.parent.type === "MemberExpression" &&
        !node.parent.computed &&
        node.parent.property === node
      ) return;
      context.report({
        node,
        messageId: "forbiddenSymbolName",
        data: { name: node.name },
      });
    };

    return {
      Identifier: reportForbiddenSymbolName,
      PrivateIdentifier: reportForbiddenSymbolName,
      JSXIdentifier: reportForbiddenSymbolName,
    };
  },
});
