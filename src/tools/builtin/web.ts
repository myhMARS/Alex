/** Built-in web tools: web_fetch / web_search (port of alex/tools/web.py, simplified). */

import type { Tool } from '../tool';
import { PERMISSION_NETWORK } from '../permissions';

const FETCH_TIMEOUT_MS = 15_000;
const DEFAULT_FETCH_CHARS = 8_000;
const MAX_PAGE_CHARS = 200_000;
const MAX_SEARCH_RESULTS = 15;
const USER_AGENT =
  'Mozilla/5.0 (compatible; AlexTS/0.1; +https://github.com/)';

export interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

/** Strip HTML tags/scripts to readable text (small, dependency-free extractor). */
export function htmlToText(html: string): string {
  return html
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, ' ')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/(?:p|div|li|tr|h[1-6]|section|article)>/gi, '\n')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&mdash;/gi, '—')
    .replace(/&ndash;/gi, '–')
    .replace(/[ \t\f\v]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function decodeDuckDuckGoUrl(href: string): string {
  try {
    const parsed = new URL(href, 'https://duckduckgo.com');
    const target = parsed.searchParams.get('uddg');
    return target ? decodeURIComponent(target) : href;
  } catch {
    return href;
  }
}

/** Parse DuckDuckGo HTML results (exported for tests). */
export function parseDuckDuckGoResults(html: string): SearchResult[] {
  const results: SearchResult[] = [];
  const linkPattern = /<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi;
  const snippetPattern = /class="[^"]*result__snippet[^"]*"[^>]*>([\s\S]*?)<\/(?:a|td|div)>/gi;
  const snippets: string[] = [];
  for (const match of html.matchAll(snippetPattern)) {
    snippets.push(htmlToText(match[1] ?? ''));
  }
  let index = 0;
  for (const match of html.matchAll(linkPattern)) {
    const href = match[1] ?? '';
    const title = htmlToText(match[2] ?? '');
    if (!title || !href) continue;
    results.push({
      title,
      url: decodeDuckDuckGoUrl(href),
      snippet: snippets[index] ?? '',
    });
    index += 1;
    if (results.length >= MAX_SEARCH_RESULTS) break;
  }
  return results;
}

export function createWebFetchTool(): Tool {
  return {
    name: 'web_fetch',
    description:
      'Fetch a URL over HTTP(S) and return readable text (HTML is converted to ' +
      'plain text, scripts/styles removed).',
    parameters: {
      type: 'object',
      properties: {
        url: { type: 'string', description: 'HTTP(S) URL to fetch' },
        max_length: {
          type: 'integer',
          description: 'Maximum characters to return (default 8000)',
        },
        max_chars: { type: 'integer', description: 'Alias for max_length' },
      },
      required: ['url'],
    },
    metadata: {
      requiredPermission: PERMISSION_NETWORK,
      summarise: (args) => `fetch ${String(args.url ?? '')}`,
    },
    async invoke(args) {
      try {
        const url = typeof args.url === 'string' ? args.url.trim() : '';
        if (!/^https?:\/\//i.test(url)) return 'Error: url must start with http:// or https://';
        const requested = typeof args.max_length === 'number' ? args.max_length : args.max_chars;
        const limit =
          typeof requested === 'number' && requested > 0
            ? Math.min(Math.floor(requested), MAX_PAGE_CHARS)
            : DEFAULT_FETCH_CHARS;

        const response = await fetch(url, {
          headers: { 'user-agent': USER_AGENT, accept: 'text/html,text/plain,*/*' },
          signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
          redirect: 'follow',
        });
        const contentType = response.headers.get('content-type') ?? '';
        const raw = await response.text();
        const body = contentType.includes('html') ? htmlToText(raw) : raw;
        const text = body.length > limit ? `${body.slice(0, limit)}\n\n[Content truncated...]` : body;
        return [
          `URL: ${url}`,
          `Status: ${response.status} ${response.statusText}`,
          `Content-Type: ${contentType || 'unknown'}`,
          '',
          text,
        ].join('\n');
      } catch (error) {
        return `Error fetching ${String(args.url ?? '')}: ${error instanceof Error ? error.message : String(error)}`;
      }
    },
  };
}

export function createWebSearchTool(): Tool {
  return {
    name: 'web_search',
    description:
      'Search the web (DuckDuckGo HTML endpoint) and return titles, URLs and snippets.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Search query' },
        max_results: {
          type: 'integer',
          description: 'Max results (default 5, max 15)',
        },
      },
      required: ['query'],
    },
    metadata: {
      requiredPermission: PERMISSION_NETWORK,
      summarise: (args) => `search ${String(args.query ?? '')}`,
    },
    async invoke(args) {
      const query = typeof args.query === 'string' ? args.query.trim() : '';
      if (!query) return 'Error: query is required';
      try {
        const response = await fetch(
          `https://html.duckduckgo.com/html/?q=${encodeURIComponent(query)}`,
          {
            headers: { 'user-agent': USER_AGENT, accept: 'text/html' },
            signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
          },
        );
        if (!response.ok) return `Error: search failed with HTTP ${response.status}`;
        const html = await response.text();
        const results = parseDuckDuckGoResults(html).slice(
          0,
          typeof args.max_results === 'number' && args.max_results > 0
            ? Math.min(Math.floor(args.max_results), MAX_SEARCH_RESULTS)
            : 5,
        );
        if (results.length === 0) return 'No results.';
        const lines = [`Search results for: ${query}`];
        results.forEach((result, index) => {
          lines.push(`${index + 1}. ${result.title}`);
          lines.push(`   ${result.url}`);
          if (result.snippet) lines.push(`   ${result.snippet}`);
        });
        return lines.join('\n');
      } catch (error) {
        return `Error searching '${query}': ${error instanceof Error ? error.message : String(error)}`;
      }
    },
  };
}