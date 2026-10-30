import type { CSSProperties } from "react";
const paths: Record<string, string> = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  chat: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l-2 2V11.5A8.5 8.5 0 0 1 10.5 3h2a8.5 8.5 0 0 1 8.5 8.5Z M7 10h10 M7 14h6",
  plugins: "M8 3v5 M16 3v5 M6 8h12v4a6 6 0 0 1-12 0V8Z M12 18v4",
  memory:
    "M12 6c-3-3-8-3-9-1v14c3-2 6-2 9 0 3-2 6-2 9 0V5c-1-2-6-2-9 1Z M12 6v13",
  tasks: "M9 4h11v17H4V4h5 M9 2h6v4H9z M8 11h8 M8 16h5",
  approvals: "M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6l-9-4Z M8 12l3 3 5-6",
  events: "M3 5h18 M3 12h18 M3 19h18 M7 3v4 M16 10v4 M10 17v4",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2",
  media: "M3 4h18v16H3z M3 16l5-5 5 5 3-3 5 5 M16 8h.01",
  life: "M12 21V10 M12 15C3 15 3 7 3 7s9-1 9 8Z M12 10c0-8 9-8 9-8s0 8-9 8Z",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  refresh: "M20 8a8 8 0 1 0 0 8 M20 3v5h-5",
};
export default function Icon({
  name,
  size = 20,
  style,
}: {
  name: string;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      style={style}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name] || paths.overview} />
    </svg>
  );
}
export function WhaleMark({ size = 42 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      aria-hidden="true"
    >
      <rect width="48" height="48" rx="15" fill="currentColor" opacity=".12" />
      <path
        d="M8 23c1 9 7 15 17 15 10 0 16-7 16-17-4 1-6 3-7 6-4-8-16-10-23-5M12 22c-2-5-1-8 2-10 4 2 6 5 5 9"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="24" cy="27" r="1.5" fill="currentColor" />
      <path
        d="M27 14v-4m4 6 2-3"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}
