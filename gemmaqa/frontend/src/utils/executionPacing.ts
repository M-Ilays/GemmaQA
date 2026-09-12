export const EXECUTION_SPEEDS = [0.25, 0.5, 1, 1.5, 2, 4] as const;
export const ACTION_PAUSES = [0, 0.5, 1, 2, 5, 10] as const;

export const DEFAULT_EXECUTION_SPEED = 1;
export const DEFAULT_ACTION_PAUSE = 0;

export type ExecutionSpeed = (typeof EXECUTION_SPEEDS)[number];
export type ActionPause = (typeof ACTION_PAUSES)[number];

export function formatSpeed(value: number): string {
  return `${value}x`;
}

export function formatPause(seconds: number): string {
  return seconds === 0 ? "0s" : `${seconds}s`;
}

export function isExecutionSpeed(value: number): value is ExecutionSpeed {
  return (EXECUTION_SPEEDS as readonly number[]).includes(value);
}

export function isActionPause(value: number): value is ActionPause {
  return (ACTION_PAUSES as readonly number[]).includes(value);
}
