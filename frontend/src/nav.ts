export interface NavItem {
  path: string;
  label: string;
  description: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { path: "/", label: "Dashboard", description: "An overview of your vault." },
  { path: "/documents", label: "Documents", description: "Import and browse your documents." },
  { path: "/search", label: "Search", description: "Find passages by keyword and meaning." },
  { path: "/ask", label: "Ask", description: "Ask questions and get cited answers." },
  { path: "/settings", label: "Settings", description: "Configure the vault and AI providers." },
];

/** Is `pathname` the section at `path` or a page inside it? `/documents-old` is not in `/documents`. */
export function isInSection(pathname: string, path: string): boolean {
  if (path === "/") {
    return pathname === "/";
  }
  return pathname === path || pathname.startsWith(`${path}/`);
}
