import { katex } from "@mdit/plugin-katex";
import MarkdownIt from "markdown-it";

export type AssistantContent =
  | { kind: "markdown"; html: string }
  | { kind: "json"; formatted: string };

const markdown = new MarkdownIt({
  breaks: true,
  html: false,
  linkify: true,
  typographer: false,
}).use(katex, {
  delimiters: "all",
  mathFence: true,
  throwOnError: false,
  trust: false,
});

// Keep protocol URLs linkable while preventing filenames such as worker.py or
// policy.md from being interpreted as fuzzy hostnames.
markdown.linkify.set({ fuzzyLink: false, fuzzyEmail: false });

markdown.validateLink = (url: string) => {
  const normalized = url.trim().toLowerCase();
  return (
    normalized.startsWith("https://") ||
    normalized.startsWith("http://") ||
    normalized.startsWith("mailto:") ||
    normalized.startsWith("#") ||
    normalized.startsWith("/") ||
    normalized.startsWith("./") ||
    normalized.startsWith("../")
  );
};

markdown.renderer.rules.link_open = (tokens, index, options, _env, self) => {
  const href = tokens[index].attrGet("href") ?? "";
  if (href.startsWith("http://") || href.startsWith("https://") || href.startsWith("mailto:")) {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
  }
  return self.renderToken(tokens, index, options);
};

export function renderAssistantContent(rawContent: string): AssistantContent {
  const content = unwrapLeakedFinishAction(rawContent.trim());
  const parsed = parseWholeJson(content);
  if (parsed !== undefined) {
    return { kind: "json", formatted: JSON.stringify(parsed, null, 2) };
  }
  return { kind: "markdown", html: markdown.render(content) };
}

function unwrapLeakedFinishAction(content: string): string {
  const parsed = parseWholeJson(content);
  if (
    isRecord(parsed) &&
    parsed.action_type === "finish" &&
    typeof parsed.final_message === "string" &&
    parsed.final_message.trim()
  ) {
    return parsed.final_message.trim();
  }
  return content;
}

function parseWholeJson(content: string): unknown | undefined {
  if (!content || !["{", "["].includes(content[0])) return undefined;
  try {
    return JSON.parse(content);
  } catch {
    return undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
