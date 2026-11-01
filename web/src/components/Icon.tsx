import type { CSSProperties } from "react";

const paths: Record<string, string> = {
  media: "M3 4h18v16H3z M3 16l5-5 5 5 3-3 5 5 M15 8h.01",
  image: "M3 4h18v16H3z M3 16l5-5 5 5 3-3 5 5 M15 8h.01",
  voice:
    "M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0z M5 10v2a7 7 0 0 0 14 0v-2 M12 19v3 M8 22h8",
  workflow: "M4 4h5v5H4z M15 15h5v5h-5z M6.5 9v8.5H15 M9 6.5h8.5V15",
  workflows: "M4 4h5v5H4z M15 15h5v5h-5z M6.5 9v8.5H15 M9 6.5h8.5V15",
  explore: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M16 8l-2 6-6 2 2-6z",

  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  studio: "M4 4h6v6H4z M14 4h6v6h-6z M9 14h6v6H9z M10 7h4 M12 10v4 M7 10v7h2",
  chat: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H3l2-5a8.5 8.5 0 1 1 16-3.5Z M8 10h8 M8 14h5",
  plugins: "M9 3h6v5h5v6h-5v7H9v-7H4V8h5z",
  memory: "M5 4h11l3 3v13H5z M8 9h8 M8 13h8 M8 17h5",
  tasks: "M9 5h11 M9 12h11 M9 19h11 M3 4l1 1 2-2 M3 11l1 1 2-2 M3 18l1 1 2-2",
  initiative:
    "M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9 M10 21a2 2 0 0 0 4 0",
  approvals: "M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7z M8 12l3 3 5-6",
  events: "M4 5h16v14H4z M7 9l3 3-3 3 M13 15h4",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 4 4 1v8l-4 1-1 4H9l-1-4-4-1V8l4-1z",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  refresh: "M20 8a8 8 0 1 0 0 8 M20 3v5h-5",
  logout: "M9 4H4v16h5 M12 12h9 M17 8l4 4-4 4",
  whale:
    "M3 13c3 0 4-1 5-4 1 5 4 7 8 6 3-1 4-4 3-7 3 1 4 4 2 8-2 5-10 6-15 2l-3 1z M14 13h.01",
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
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={style}
    >
      <path d={paths[name] ?? paths.overview} />
    </svg>
  );
}
