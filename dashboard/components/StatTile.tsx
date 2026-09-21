/**
 * A hero number. Used instead of a chart because each of these is a single
 * headline figure — a chart of one number is decoration.
 */
export function StatTile({
  label,
  value,
  note,
  unknown = false,
}: {
  label: string;
  value: string;
  note?: string;
  unknown?: boolean;
}) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className={unknown ? "value unknown" : "value"}>{value}</div>
      {note ? <div className="note">{note}</div> : null}
    </div>
  );
}
