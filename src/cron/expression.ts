/** 5-field cron expression parsing and next-fire computation. */

export class CronParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CronParseError';
  }
}

interface CronSpec {
  minutes: Set<number>;
  hours: Set<number>;
  days: Set<number>;
  months: Set<number>;
  weekdays: Set<number>;
}

const FIELD_RANGES: Array<{ name: string; min: number; max: number; values: keyof CronSpec }> = [
  { name: 'minute', min: 0, max: 59, values: 'minutes' },
  { name: 'hour', min: 0, max: 23, values: 'hours' },
  { name: 'day', min: 1, max: 31, values: 'days' },
  { name: 'month', min: 1, max: 12, values: 'months' },
  { name: 'day_of_week', min: 0, max: 7, values: 'weekdays' },
];

function parseField(raw: string, name: string, min: number, max: number): Set<number> {
  const values = new Set<number>();
  for (const part of raw.split(',')) {
    const token = part.trim();
    if (!token) throw new CronParseError(`Empty ${name} field`);
    const [rangePart, stepPart] = token.split('/');
    const step = stepPart === undefined ? 1 : Number(stepPart);
    if (!Number.isInteger(step) || step <= 0) {
      throw new CronParseError(`Invalid step in ${name}: ${token}`);
    }
    let start: number;
    let end: number;
    if (rangePart === '*') {
      start = min;
      end = max;
    } else if (rangePart?.includes('-')) {
      const [startRaw, endRaw] = rangePart.split('-');
      start = Number(startRaw);
      end = Number(endRaw);
    } else {
      start = Number(rangePart);
      end = stepPart === undefined ? start : max;
    }
    if (!Number.isInteger(start) || !Number.isInteger(end)) {
      throw new CronParseError(`Invalid ${name} value: ${token}`);
    }
    if (start < min || end > max || start > end) {
      throw new CronParseError(`${name} out of range: ${token}`);
    }
    for (let value = start; value <= end; value += step) values.add(value);
  }
  return values;
}

export function parseCron(expression: string): CronSpec {
  const parts = (expression ?? '').trim().split(/\s+/).filter(Boolean);
  if (parts.length !== 5) {
    throw new CronParseError('Cron must have exactly 5 fields: minute hour day month day_of_week');
  }
  const spec: CronSpec = {
    minutes: new Set(),
    hours: new Set(),
    days: new Set(),
    months: new Set(),
    weekdays: new Set(),
  };
  FIELD_RANGES.forEach((field, index) => {
    const values = parseField(parts[index]!, field.name, field.min, field.max);
    if (field.values === 'weekdays') {
      for (const value of values) spec.weekdays.add(value === 7 ? 0 : value);
    } else {
      for (const value of values) spec[field.values].add(value);
    }
  });
  return spec;
}

function matches(date: Date, spec: CronSpec): boolean {
  return (
    spec.minutes.has(date.getMinutes()) &&
    spec.hours.has(date.getHours()) &&
    spec.days.has(date.getDate()) &&
    spec.months.has(date.getMonth() + 1) &&
    spec.weekdays.has(date.getDay())
  );
}

/** Next fire time (epoch ms strictly after `afterMs`), local timezone. */
export function nextCronTime(afterMs: number, expression: string): number {
  const spec = parseCron(expression);
  const candidate = new Date(afterMs);
  candidate.setSeconds(0, 0);
  candidate.setMinutes(candidate.getMinutes() + 1);

  const limit = afterMs + 4 * 366 * 24 * 60 * 60 * 1000;
  while (candidate.getTime() <= limit) {
    if (matches(candidate, spec)) return candidate.getTime();
    candidate.setMinutes(candidate.getMinutes() + 1);
  }
  throw new CronParseError('No next fire time found in the next 4 years');
}

/** Validate a cron expression; throws CronParseError when malformed. */
export function validateCron(expression: string): void {
  nextCronTime(Date.now(), expression);
}