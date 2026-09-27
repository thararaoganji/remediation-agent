// Shield + checkmark: "issues found, verified fixed" -- the whole point of
// the three agents this dashboard drives. Plain inline SVG, no asset file
// or icon library needed, and it inherits currentColor nowhere (kept as
// explicit brand blue) so it reads the same in light and dark mode.
export default function Logo({ size = 28 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      role="img"
      aria-label="Sonar Remediation Dashboard logo"
    >
      <path
        d="M24 4L40 10.5V22C40 32.8 33.2 40.7 24 44C14.8 40.7 8 32.8 8 22V10.5L24 4Z"
        fill="#1a73e8"
      />
      <path
        d="M24 4L40 10.5V22C40 32.8 33.2 40.7 24 44V4Z"
        fill="#1558b3"
      />
      <path
        d="M15.5 23.5L21 29L32.5 17"
        stroke="#fff"
        strokeWidth="3.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  )
}
