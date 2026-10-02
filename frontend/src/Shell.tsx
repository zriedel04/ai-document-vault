import { AppShell, Burger, Group, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { Outlet, useLocation } from "@tanstack/react-router";

import { useHealthState } from "./api/health-context";
import { ColorSchemeToggle } from "./components/ColorSchemeToggle";
import { HealthBadge } from "./components/HealthBadge";
import { RouterNavLink } from "./components/router-nav-link";
import { NAV_ITEMS, isInSection } from "./nav";

/** The frame around every page: header, navigation and the page itself. */
export function Shell() {
  const [opened, { toggle, close }] = useDisclosure();
  const health = useHealthState();
  const { pathname } = useLocation();

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
            <RouterNavLink
              key={item.path}
              to={item.path}
              activeOptions={{ exact: item.path === "/" }}
              active={isInSection(pathname, item.path)}
              label={item.label}
              onClick={close}
            />
          ))}
        </nav>
      </AppShell.Navbar>

      <AppShell.Main>
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
