import type { ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

function safeHref(href: string | undefined) {
  if (!href) return null;
  try {
    const url = new URL(href);
    return url.protocol === "https:" || url.protocol === "http:" ? href : null;
  } catch {
    return null;
  }
}

/**
 * Display-only rich text for an assistant's narrative.
 *
 * Raw HTML is deliberately unsupported. Financial records and actions remain
 * typed widgets; Markdown is only the explanatory layer around those widgets.
 */
export function MarkdownMessage({ children }: { children: string }) {
  return <div className="markdown-message min-w-0 text-body leading-6 text-ink-body">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
        h1: ({ children }) => <h2 className="mt-4 mb-2 font-heading text-title font-semibold text-ink">{children}</h2>,
        h2: ({ children }) => <h3 className="mt-4 mb-2 font-heading text-title font-semibold text-ink">{children}</h3>,
        h3: ({ children }) => <h4 className="mt-3 mb-2 font-heading text-control font-semibold text-ink">{children}</h4>,
        ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-4 marker:text-secondary">{children}</ul>,
        ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-4 marker:font-semibold marker:text-secondary">{children}</ol>,
        li: ({ children }) => <li className="pl-0.5">{children}</li>,
        blockquote: ({ children }) => <blockquote className="my-3 border-l-2 border-secondary-line pl-3 text-ink-muted">{children}</blockquote>,
        strong: ({ children }) => <strong className="font-semibold text-ink">{children}</strong>,
        code: ({ children, className, ...props }: ComponentPropsWithoutRef<"code">) => <code className={`${className ?? ""} rounded bg-surface-sunken px-1 py-0.5 font-mono text-[0.88em] text-ink`} {...props}>{children}</code>,
        pre: ({ children }) => <pre className="my-3 max-w-full overflow-x-auto rounded-xl bg-ink p-3 text-note leading-5 text-surface [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-inherit">{children}</pre>,
        a: ({ href, children }) => {
          const safe = safeHref(href);
          return safe ? <a href={safe} target="_blank" rel="noreferrer noopener" className="font-medium text-secondary underline decoration-secondary-line underline-offset-2">{children}</a> : <span>{children}</span>;
        },
        table: ({ children }) => <span tabIndex={0} role="region" aria-label="Scrollable table" className="my-3 block max-h-[28rem] max-w-full overflow-auto overscroll-auto rounded-xl border border-line focus-visible:outline-2 focus-visible:outline-secondary"><table className="w-full min-w-[420px] border-collapse text-left text-note">{children}</table></span>,
        thead: ({ children }) => <thead className="bg-surface-sunken text-meta tracking-[0.08em] text-ink-muted uppercase">{children}</thead>,
        th: ({ children }) => <th className="border-b border-line px-3 py-2 font-semibold">{children}</th>,
        td: ({ children }) => <td className="border-b border-line px-3 py-2 align-top last:text-right">{children}</td>,
      }}
    >{children}</ReactMarkdown>
  </div>;
}
