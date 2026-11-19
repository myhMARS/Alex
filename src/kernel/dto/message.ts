/** OpenAI-compatible message dicts (mirrors alex/messages.py). */

export interface ToolCallFunctionDTO {
  name?: string;
  arguments?: string;
  [key: string]: unknown;
}

export interface ToolCallDTO {
  id?: string;
  type?: string;
  function?: ToolCallFunctionDTO;
  [key: string]: unknown;
}

export type AttachmentKind = 'image' | 'file';

/** A user attachment carried with a turn and rendered into model content parts. */
export interface AttachmentDTO {
  kind: AttachmentKind;
  name: string;
  path?: string;
  mime?: string;
  size: number;
  /** Data URL for images. */
  dataUrl?: string;
  /** UTF-8 text for text-like files. */
  text?: string;
  /** Content-addressed hash for stored images. */
  hash?: string;
  width?: number;
  height?: number;
}

export type ContentPart =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } };

export interface MessageDTO {
  role: string;
  content?: string | ContentPart[];
  reasoning_content?: string;
  tool_calls?: ToolCallDTO[];
  tool_call_id?: string;
  [key: string]: unknown;
}

export function userMessage(content: string | ContentPart[]): MessageDTO {
  return { role: 'user', content };
}

export function assistantMessage(content = '', extra: Partial<MessageDTO> = {}): MessageDTO {
  return { role: 'assistant', content, ...extra };
}

export function toolMessage(content: string, toolCallId: string): MessageDTO {
  return { role: 'tool', content, tool_call_id: toolCallId };
}