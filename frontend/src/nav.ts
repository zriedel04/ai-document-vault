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
