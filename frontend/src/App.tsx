import { AppShell, Burger, Group, NavLink, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { NavLink as RouterLink, Route, Routes } from "react-router-dom";

import type { Api } from "./api/client";
import { useHealth } from "./api/useHealth";
import { ColorSchemeToggle } from "./components/ColorSchemeToggle";
import { HealthBadge } from "./components/HealthBadge";
import { NAV_ITEMS } from "./nav";
import { Dashboard } from "./pages/Dashboard";
import { NotFound } from "./pages/NotFound";
import { Placeholder } from "./pages/Placeholder";

export function App({ api }: { api: Api }) {
  const [opened, { toggle, close }] = useDisclosure();
  const health = useHealth(api);

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 220, breakpoint: "sm", collapsed: { mobile: !opened } }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Menu" />
            <Title order={3}>AI Document Vault</Title>
          </Group>
          <Group>
            <HealthBadge health={health} />
            <ColorSchemeToggle />
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="xs">
        <nav aria-label="Main">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.path}
              component={RouterLink}
              to={item.path}
              end={item.path === "/"}
              label={item.label}
              onClick={close}
            />
          ))}
        </nav>
      </AppShell.Navbar>

      <AppShell.Main>
        <Routes>
          <Route path="/" element={<Dashboard health={health} />} />
          {NAV_ITEMS.filter((item) => item.path !== "/").map((item) => (
            <Route key={item.path} path={item.path} element={<Placeholder item={item} />} />
          ))}
          <Route path="*" element={<NotFound />} />
        </Routes>
      </AppShell.Main>
    </AppShell>
  );
}
