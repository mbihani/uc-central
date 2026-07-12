import { Link } from "@tanstack/react-router";

interface LogoProps {
  to?: string;
  className?: string;
  showText?: boolean;
}

function DatabricksLogo({ className = "h-7 w-7" }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 100 100"
      className={className}
      fill="none"
      aria-label="Databricks"
    >
      <g
        stroke="#FF3621"
        strokeWidth="7"
        strokeLinejoin="round"
        strokeLinecap="round"
      >
        <polygon points="50,4 95,28 50,52 5,28" />
        <polyline points="5,46 50,70 95,46" />
        <polyline points="5,66 50,90 95,66" />
      </g>
    </svg>
  );
}

export function Logo({ to = "/", className = "", showText = true }: LogoProps) {
  const content = (
    <div className={`flex items-center gap-2.5 ${className}`}>
      <DatabricksLogo className="h-7 w-7" />
      {showText && (
        <span className="font-semibold text-base text-sidebar-foreground">
          {__APP_NAME__}
        </span>
      )}
    </div>
  );

  if (to) {
    return (
      <Link to={to} className="hover:opacity-80 transition-opacity">
        {content}
      </Link>
    );
  }

  return content;
}

export default Logo;
