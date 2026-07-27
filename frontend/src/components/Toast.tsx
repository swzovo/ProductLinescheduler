export function Toast({
  message,
  kind = "success",
  onClose,
}: {
  message: string;
  kind?: "success" | "error";
  onClose: () => void;
}) {
  return (
    <div className={`toast ${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span>{kind === "success" ? "✓" : "!"}</span>
      <p>{message}</p>
      <button onClick={onClose} aria-label="关闭提示">
        ×
      </button>
    </div>
  );
}

