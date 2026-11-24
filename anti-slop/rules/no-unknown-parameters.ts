import { defineRule } from "@oxlint/plugins";
import type { ESTree } from "@oxlint/plugins";

type Parameter = ESTree.ParamPattern;

type ParameterOwner =
  | ESTree.ArrowFunctionExpression
  | ESTree.Function
  | ESTree.TSCallSignatureDeclaration
  | ESTree.TSConstructSignatureDeclaration
  | ESTree.TSConstructorType
  | ESTree.TSFunctionType
  | ESTree.TSMethodSignature;

const unknownAnnotationSuffix = /\s*:\s*unknown\s*$/u;

function nestedParameter(parameter: Parameter): Parameter | null {
  if (parameter.type === "TSParameterProperty") return parameter.parameter;

  if (parameter.type === "RestElement") return parameter.argument;

  if (parameter.type === "AssignmentPattern") return parameter.left;

  return null;
}

function ownAnnotation(parameter: Parameter): ESTree.TSTypeAnnotation | null | undefined {
  return "typeAnnotation" in parameter ? parameter.typeAnnotation : undefined;
}

function parameterAnnotation(parameter: Parameter): ESTree.TSTypeAnnotation | null | undefined {
  const nested = nestedParameter(parameter);

  return ownAnnotation(parameter) ?? (nested === null ? undefined : parameterAnnotation(nested));
}

function parameterName(parameter: Parameter, sourceText: string): string {
  const nested = nestedParameter(parameter);

  if (nested !== null) return parameterName(nested, sourceText);

  return parameter.type === "Identifier"
    ? parameter.name
    : sourceText.replace(unknownAnnotationSuffix, "");
}

/** Disallow unknown inputs except explicitly named error-cause enrichment. */
export const noUnknownParametersRule = defineRule({
  meta: {
    type: "problem",
    docs: {
      description:
        "Disallow explicitly unknown function parameters except `cause`; decode unknown input at its I/O boundary instead.",
    },
    messages: {
      unknownParameter:
        "Parameter `{{parameter}}` leaves input unparsed. Accept a named domain type; run the expected schema or parser at the I/O boundary before calling this function.",
    },
  },
  createOnce(context) {
    const checkParameters = (node: ParameterOwner) => {
      for (const parameter of node.params) {
        const annotation = parameterAnnotation(parameter);

        if (annotation?.typeAnnotation.type !== "TSUnknownKeyword") continue;

        const name = parameterName(parameter, context.sourceCode.getText(parameter));

        if (name === "cause") continue;

        context.report({
          node: annotation.typeAnnotation,
          messageId: "unknownParameter",
          data: { parameter: name },
        });
      }
    };

    return {
      ArrowFunctionExpression: checkParameters,
      FunctionDeclaration: checkParameters,
      FunctionExpression: checkParameters,
      TSCallSignatureDeclaration: checkParameters,
      TSConstructSignatureDeclaration: checkParameters,
      TSConstructorType: checkParameters,
      TSDeclareFunction: checkParameters,
      TSEmptyBodyFunctionExpression: checkParameters,
      TSFunctionType: checkParameters,
      TSMethodSignature: checkParameters,
    };
  },
});
