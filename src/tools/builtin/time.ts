/** Built-in `time` tool. */

import type { Tool } from '../tool';

export function createTimeTool(): Tool {
  return {
    name: 'time',
    description:
      'Get the current date and time. Returns a human-readable timestamp plus ISO 8601, ' +
      "optionally for a named timezone such as 'Asia/Shanghai', 'US/Eastern' or 'UTC'.",
    parameters: {
      type: 'object',
      properties: {
        timezone: {
          type: 'string',
          description:
            "Timezone name (IANA), or 'local' for the system timezone (default)",
        },
      },
      required: [],
    },
    async invoke(args) {
      const raw = typeof args.timezone === 'string' ? args.timezone.trim() : 'local';
      const useLocal = !raw || raw.toLowerCase() === 'local';
      const now = new Date();
      try {
        const formatter = new Intl.DateTimeFormat('en-GB', {
          timeZone: useLocal ? undefined : raw,
          dateStyle: 'full',
          timeStyle: 'long',
        });
        return [
          `Current time: ${formatter.format(now)}`,
          `Timezone: ${useLocal ? 'local' : raw}`,
          `ISO 8601: ${now.toISOString()}`,
        ].join('\n');
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return `Error getting current time: ${message}`;
      }
    },
    metadata: {},
  };
}