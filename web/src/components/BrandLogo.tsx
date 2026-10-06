import wordmark from "../assets/limbowave-logo-transparent.svg?no-inline";

/** The supplied two-line wordmark; never recreate branding with text or paths. */
export function BrandLogo({
  variant = "header",
}: {
  variant?: "header" | "panel" | "hero" | "message";
}) {
  return (
    <img
      className={`brand-logo brand-logo--${variant}`}
      src={wordmark}
      alt="LimboWave"
      width={1161}
      height={710}
      draggable={false}
    />
  );
}
