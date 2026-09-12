import {
  ACTION_PAUSES,
  EXECUTION_SPEEDS,
  formatPause,
  formatSpeed,
} from "../utils/executionPacing";

export function ExecutionPacingControls({
  speed,
  pause,
  disabled,
  onSpeed,
  onPause,
}: {
  speed: number;
  pause: number;
  disabled?: boolean;
  onSpeed: (value: number) => void;
  onPause: (value: number) => void;
}) {
  return (
    <div className="surface space-y-3 p-4">
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">
          Execution Speed
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {EXECUTION_SPEEDS.map((value) => (
            <Chip
              key={value}
              label={formatSpeed(value)}
              active={value === speed}
              disabled={disabled}
              onClick={() => onSpeed(value)}
            />
          ))}
        </div>
      </div>
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em] text-slate-500">
          Action Pause
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {ACTION_PAUSES.map((value) => (
            <Chip
              key={value}
              label={formatPause(value)}
              active={value === pause}
              disabled={disabled}
              onClick={() => onPause(value)}
            />
          ))}
        </div>
      </div>
      <p className="text-xs text-slate-500">
        Speed: {formatSpeed(speed)} · Action pause: {formatPause(pause)}
      </p>
    </div>
  );
}

function Chip({
  label,
  active,
  disabled,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`rounded-full px-2.5 py-1 text-xs ${
        active
          ? "bg-tide-500/30 text-tide-100 ring-1 ring-tide-400/70"
          : "bg-white/5 text-slate-400 hover:text-slate-200"
      } disabled:cursor-not-allowed disabled:opacity-50`}
    >
      {label}
    </button>
  );
}
